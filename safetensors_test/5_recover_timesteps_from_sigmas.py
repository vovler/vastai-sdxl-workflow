#!/usr/bin/env python3
import torch
from diffusers import (
    StableDiffusionXLPipeline,
    UNet2DConditionModel,
    AutoencoderKL,
    EulerDiscreteScheduler,
)
from transformers import CLIPTokenizer, CLIPTextModel, CLIPTextModelWithProjection
from safetensors.torch import load_file
from pathlib import Path
import sys
from PIL import Image
import shutil
import time
from tqdm import tqdm
import numpy as np

def main():
    """
    Generates an image with SDXL using an unfused UNet, a separate VAE, and a LoRA.
    The process is run manually to decode the UNet output with the VAE explicitly.
    """
    with torch.no_grad():
        if not torch.cuda.is_available():
            print("Error: CUDA is not available. This script requires a GPU.")
            sys.exit(1)

        # --- Configuration ---
        base_dir = Path("/lab/model")
        device = "cuda"
        dtype = torch.float16

        #prompt = "masterpiece,best quality,amazing quality, general, 1girl, aqua_(konosuba), on a swing, looking at viewer, volumetric_lighting, park, night, shiny clothes, shiny skin, detailed_background"
        prompt = "masterpiece,best quality,amazing quality, general, 1girl, aqua_(konosuba), face_focus, looking at viewer, volumetric_lighting"
        
        # Pipeline settings
        cfg_scale = 1.0
        num_inference_steps = 8
        seed = 1020094661
        generator = torch.Generator(device="cuda").manual_seed(seed)
        height = 832
        width = 1216
        batch_size = 1
        
        # --- Load Model Components ---
        print("=== Loading models ===")
        
        # Load VAE from its own directory
        print("Loading VAE...")
        vae = AutoencoderKL.from_pretrained(
            base_dir / "vae",
            torch_dtype=dtype
        )
        vae.to(device)

        # Load text encoders and tokenizers
        print("Loading text encoders and tokenizers...")
        tokenizer = CLIPTokenizer.from_pretrained(str(base_dir), subfolder="tokenizer")
        tokenizer_2 = CLIPTokenizer.from_pretrained(str(base_dir), subfolder="tokenizer_2")
        text_encoder = CLIPTextModel.from_pretrained(
            str(base_dir), subfolder="text_encoder", torch_dtype=dtype, use_safetensors=True
        )
        text_encoder.to(device)
        text_encoder_2 = CLIPTextModelWithProjection.from_pretrained(
            str(base_dir), subfolder="text_encoder_2", torch_dtype=dtype, use_safetensors=True
        )
        text_encoder_2.to(device)

        # Load the unfused UNet weights
        print("Loading unfused UNet...")
        unet_dir = base_dir / "unet"

        unet = UNet2DConditionModel.from_pretrained(
            str(unet_dir), torch_dtype=dtype, use_safetensors=True
        )
        unet.to(device)
        print("✓ Unfused UNet loaded.")

        # Create the scheduler
        scheduler = EulerDiscreteScheduler.from_config(
            str(base_dir / "scheduler")
        )
        print(f"✓ Scheduler set to EulerDiscreteScheduler.")
        
        # Instantiate pipeline from components
        print("Instantiating pipeline from components...")
        pipe = StableDiffusionXLPipeline(
            vae=vae,
            text_encoder=text_encoder,
            text_encoder_2=text_encoder_2,
            tokenizer=tokenizer,
            tokenizer_2=tokenizer_2,
            unet=unet,
            scheduler=scheduler,
        )
        pipe.enable_xformers_memory_efficient_attention()
        pipe.enable_vae_tiling()
        pipe.enable_vae_slicing()


        # --- Manual Inference Process ---
        print("\n=== Starting Manual Inference ===")
        # 1. Encode prompts
        print("Encoding prompts...")
        (
            prompt_embeds,
            _,
            pooled_prompt_embeds,
            _,
        ) = pipe.encode_prompt(
            prompt,
            device=device,
            num_images_per_prompt=batch_size,
            do_classifier_free_guidance=1
        )

        # 2. Prepare latents
        print("Preparing latents...")
        latents = torch.randn(
            (batch_size, pipe.unet.config.in_channels, height // 8, width // 8),
            generator=generator,
            device=device,
            dtype=dtype,
        )
        # Scale the initial noise by the scheduler's standard deviation
        latents = latents * pipe.scheduler.init_noise_sigma

        # 3. Prepare timesteps from custom sigmas
        custom_sigmas_np = np.array([7.371844291687012, 4.116696357727051, 2.5109214782714844, 1.6236920356750488, 1.0760310888290405, 0.6983981132507324, 0.4022177457809448, 0.04131441190838814, 0.0])
        num_inference_steps = len(custom_sigmas_np) - 1
        
        custom_sigmas = torch.from_numpy(custom_sigmas_np).to(device=device, dtype=dtype)
        
        # The scheduler's full list of sigmas, used for lookup
        scheduler_sigmas = scheduler.sigmas.to(device=device, dtype=dtype)

        # For each of our custom sigmas, find the closest timestep in the scheduler's original list of timesteps
        timesteps_list = []
        for sigma in custom_sigmas[:-1]: # Exclude the last sigma (0.0) as it's the destination, not a step
            idx = torch.argmin(torch.abs(scheduler_sigmas - sigma))
            timesteps_list.append(scheduler.timesteps[idx])
        
        timesteps = torch.tensor(timesteps_list, device=device)
        
        print(f"Using {num_inference_steps} inference steps.")
        print(f"Using custom sigmas: {custom_sigmas_np.tolist()}")
        print(f"Recovered timesteps: {timesteps.cpu().numpy().tolist()}")

        exit()
        pipe.scheduler.set_timesteps(num_inference_steps, device=device)
        
        # Overwrite with our recovered timesteps and custom sigmas
        pipe.scheduler.timesteps = timesteps
        pipe.scheduler.sigmas = custom_sigmas
        
        add_time_ids = pipe._get_add_time_ids((height, width), (0,0), (height, width), dtype, text_encoder_projection_dim=text_encoder_2.config.projection_dim).to(device)
        add_time_ids = add_time_ids.repeat(batch_size, 1)
        
        # 4. Denoising loop
        print(f"Running denoising loop for {num_inference_steps} steps...")
        start_time = time.time()
        for i, t in enumerate(tqdm(timesteps)):
            # No CFG for cfg_scale=1.0, so we don't duplicate inputs
            latent_model_input = latents
            
            latent_model_input = pipe.scheduler.scale_model_input(latent_model_input, t)
            
            # Prepare added conditioning signals
            added_cond_kwargs = {"text_embeds": pooled_prompt_embeds, "time_ids": add_time_ids}

            # Predict the noise residual
            noise_pred = pipe.unet(
                latent_model_input,
                t,
                encoder_hidden_states=prompt_embeds,
                cross_attention_kwargs=None,
                added_cond_kwargs=added_cond_kwargs,
                return_dict=False,
            )[0]
            
            # No guidance is applied since cfg_scale is 1.0

            # Compute the previous noisy sample x_t -> x_{t-1}
            latents = pipe.scheduler.step(noise_pred, t, latents, generator=generator, return_dict=False)[0]
        
        end_time = time.time()
        print(f"Denoising loop took: {end_time - start_time:.4f} seconds")
        print("✓ Denoising loop complete.")
        
        # 5. Manually decode the latents with the VAE
        print("Decoding latents with VAE...")

        # --- VAE Debugging ---
        print(f"VAE dtype: {pipe.vae.dtype}")
        needs_upcasting = pipe.vae.dtype == torch.float16 and getattr(pipe.vae.config, "force_upcast", False)
        print(f"Needs upcasting (pipeline logic): {needs_upcasting}")
        print("Is 'upcast_vae' being run? No, because we are calling vae.decode() manually.")

        latents_for_vae = latents / pipe.vae.config.scaling_factor
        print(f"Latents to VAE - Shape: {latents_for_vae.shape}, DType: {latents_for_vae.dtype}")
        
        # The VAE scales the latents internally
        start_time = time.time()
        image = pipe.vae.decode(latents_for_vae, return_dict=False)[0]
        end_time = time.time()
        print(f"VAE decoding took: {end_time - start_time:.4f} seconds")
        
        print(f"Image from VAE - Shape: {image.shape}, DType: {image.dtype}")

        # 6. Post-process the image
        images = pipe.image_processor.postprocess(image, output_type="pil")
        
        print("✓ Images generated successfully!")
        
        # 7. Save the images
        script_name = Path(__file__).stem
        i = 0
        for image in images:
            while True:
                output_path = f"{script_name}__{i:04d}.png"
                if not Path(output_path).exists():
                    break
                i += 1
            image.save(output_path)
            print(f"✓ Image saved to: {output_path}")
            i += 1

if __name__ == "__main__":
    main()
