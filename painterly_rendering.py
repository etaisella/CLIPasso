import warnings

warnings.filterwarnings('ignore')
warnings.simplefilter('ignore')

import argparse
import math
import os
import sys
import time
import traceback

import numpy as np
import PIL
import torch
import torch.nn as nn
import torch.nn.functional as F
import wandb
from PIL import Image
from torchvision import models, transforms
from tqdm.auto import tqdm, trange

import config
import sketch_utils as utils
from models.loss import Loss
from models.painter_params import Painter, PainterOptimizer
from IPython.display import display, SVG


def load_renderer(args, target_im=None, mask=None):
    renderer = Painter(num_strokes=args.num_paths, args=args,
                       num_segments=args.num_segments,
                       imsize=args.image_scale,
                       device=args.device,
                       target_im=target_im,
                       pixelArt=args.pixelArt,
                       mask=mask)
    renderer = renderer.to(args.device)
    return renderer


def get_target(args):
    target = Image.open(args.target)
    if target.mode == "RGBA":
        # Create a white rgba background
        new_image = Image.new("RGBA", target.size, "WHITE")
        # Paste the image on the background.
        new_image.paste(target, (0, 0), target)
        target = new_image
    target = target.convert("RGB")
    masked_im, mask = utils.get_mask_u2net(args, target)
    if args.mask_object:
        target = masked_im
    if args.fix_scale:
        target = utils.fix_image_scale(target)

    transforms_ = []
    if target.size[0] != target.size[1]:
        transforms_.append(transforms.Resize(
            (args.image_scale, args.image_scale), interpolation=PIL.Image.BICUBIC))
    else:
        transforms_.append(transforms.Resize(
            args.image_scale, interpolation=PIL.Image.BICUBIC))
        transforms_.append(transforms.CenterCrop(args.image_scale))
    transforms_.append(transforms.ToTensor())
    data_transforms = transforms.Compose(transforms_)
    target_ = data_transforms(target).unsqueeze(0).to(args.device)
    return target_, mask


def main(args):
    loss_func = Loss(args)
    inputs, mask = get_target(args)
    utils.log_input(args.use_wandb, 0, inputs, args.output_dir)
    renderer = load_renderer(args, inputs, mask)

    optimizer = PainterOptimizer(args, renderer)
    counter = 0
    configs_to_save = {"loss_eval": []}
    best_loss, best_fc_loss = 100, 100
    best_iter, best_iter_fc = 0, 0
    min_delta = 1e-5
    terminate = False

    renderer.set_random_noise(0)
    img = renderer.init_image(stage=0)
    optimizer.init_optimizers()
    
    # not using tdqm for jupyter demo
    if args.display:
        epoch_range = range(args.num_iter)
    else:
        epoch_range = tqdm(range(args.num_iter))
    
    #############################
    #####    MAIN   LOOP    #####
    #############################
    
    for epoch in epoch_range:
        if not args.display:
            epoch_range.refresh()
        renderer.set_random_noise(epoch)
        if args.lr_scheduler:
            optimizer.update_lr(epoch)

        start = time.time()
        optimizer.zero_grad_()
        
        if args.pixelArt:
            if epoch == 100:
                renderer.doColorQuantization = 1
                for g in optimizer.points_optim.param_groups:
                    g['lr'] = 2.0

            PAimage = renderer.get_PA_image().to(args.device)
            losses_dict = loss_func(PAimage, inputs.detach(
            ), renderer.get_color_parameters(), renderer, counter, optimizer)
        else:
            sketches = renderer.get_image().to(args.device)
            losses_dict = loss_func(sketches, inputs.detach(
            ), renderer.get_color_parameters(), renderer, counter, optimizer)
            
        if epoch == 0: # print original pallet at first iteration
            pallet = renderer.get_centers().to(args.device)
            utils.plot_pallet(pallet, args.output_dir, epoch, use_wandb=args.use_wandb, 
                              title=f"best_iter_h{args.canvasH}_w{args.canvasW}_quantColors{args.quantizeColors}_{args.numColors}_l2w{args.perceptual_weight}_sem_w{args.clip_fc_loss_weight}_colorLearning{args.learnColors}_original_pallet.jpg")
            
        loss = sum(list(losses_dict.values()))
        loss.backward()
        optimizer.step_(epoch)
        if epoch % args.save_interval == 0:
            if args.pixelArt:
                utils.plot_batch(inputs, PAimage, f"{args.output_dir}/jpg_logs", epoch,
                                use_wandb=args.use_wandb, title=f"PA_iter{epoch}.jpg")
                utils.plot_pallet(pallet, args.output_dir, counter, use_wandb=args.use_wandb, title=f"PA_iter{epoch}_pallet.jpg")
            else:
                utils.plot_batch(inputs, sketches, f"{args.output_dir}/jpg_logs", epoch,
                                use_wandb=args.use_wandb, title=f"iter{epoch}.jpg")
            renderer.save_svg(
                f"{args.output_dir}/svg_logs", f"svg_iter{epoch}")
        if epoch % args.eval_interval == 0:
            
            with torch.no_grad():
                if args.pixelArt:
                    losses_dict_eval = loss_func(PAimage, inputs, renderer.get_color_parameters(
                    ), renderer.get_points_parans(), counter, optimizer, mode="eval")
                    renderer.add_noise_to_weights()
                else:
                    losses_dict_eval = loss_func(sketches, inputs, renderer.get_color_parameters(
                    ), renderer.get_points_parans(), counter, optimizer, mode="eval")
                loss_eval = sum(list(losses_dict_eval.values()))
                configs_to_save["loss_eval"].append(loss_eval.item())
                for k in losses_dict_eval.keys():
                    if k not in configs_to_save.keys():
                        configs_to_save[k] = []
                    configs_to_save[k].append(losses_dict_eval[k].item())
                if args.clip_fc_loss_weight:
                    if losses_dict_eval["fc"].item() < best_fc_loss:
                        best_fc_loss = losses_dict_eval["fc"].item(
                        ) / args.clip_fc_loss_weight
                        best_iter_fc = epoch
                # print(
                #     f"eval iter[{epoch}/{args.num_iter}] loss[{loss.item()}] time[{time.time() - start}]")

                cur_delta = loss_eval.item() - best_loss
                if abs(cur_delta) > min_delta:
                    if cur_delta < 0:
                        best_loss = loss_eval.item()
                        best_iter = epoch
                        terminate = False
                        if args.pixelArt:
                            pallet = renderer.get_centers().to(args.device)
                            utils.plot_batch(
                                inputs, PAimage, args.output_dir, epoch, use_wandb=args.use_wandb, title=f"best_iter_h{args.canvasH}_w{args.canvasW}_quantColors{args.quantizeColors}_{args.numColors}_l2w{args.perceptual_weight}_sem_w{args.clip_fc_loss_weight}_colorLearning{args.learnColors}.jpg")
                            utils.plot_pallet(
                                pallet, args.output_dir, counter, use_wandb=args.use_wandb, title=f"best_iter_h{args.canvasH}_w{args.canvasW}_quantColors{args.quantizeColors}_{args.numColors}_l2w{args.perceptual_weight}_sem_w{args.clip_fc_loss_weight}_colorLearning{args.learnColors}_pallet.jpg")
                        else:
                            utils.plot_batch(
                                inputs, sketches, args.output_dir, epoch, use_wandb=args.use_wandb, title="best_iter.jpg")
                            
                        renderer.save_svg(args.output_dir, "best_iter")

                if args.use_wandb:
                    wandb.run.summary["best_loss"] = best_loss
                    wandb.run.summary["best_loss_fc"] = best_fc_loss
                    wandb_dict = {"delta": cur_delta,
                                  "loss_eval": loss_eval.item()}
                    for k in losses_dict_eval.keys():
                        wandb_dict[k + "_eval"] = losses_dict_eval[k].item()
                    wandb.log(wandb_dict, step=counter)

                if abs(cur_delta) <= min_delta:
                    if terminate:
                        break
                    terminate = True

        if args.use_wandb:
            wandb_dict = {"loss": loss.item(), "lr": optimizer.get_lr()}
            for k in losses_dict.keys():
                wandb_dict[k] = losses_dict[k].item()
            wandb.log(wandb_dict, step=counter)

        counter += 1

    return configs_to_save

if __name__ == "__main__":
    args = config.parse_arguments()
    final_config = vars(args)
    try:
        configs_to_save = main(args)
    except BaseException as err:
        print(f"Unexpected error occurred:\n {err}")
        print(traceback.format_exc())
        sys.exit(1)
    for k in configs_to_save.keys():
        final_config[k] = configs_to_save[k]
    np.save(f"{args.output_dir}/config.npy", final_config)
    if args.use_wandb:
        wandb.finish()
