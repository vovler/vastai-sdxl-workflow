import torch
from diffusers import StableDiffusionXLPipeline, UNet2DConditionModel
from torch.export import Dim
import os
from torch.onnx import _flags


class UNetWrapper(torch.nn.Module):
    def __init__(self, unet):
        super().__init__()
        self.unet = unet

    def forward(self, sample, timestep, encoder_hidden_states, text_embeds, time_ids):
        print("\n--- Exporting UNet with the following input shapes and types ---")
        print(f"  - sample:                dtype={sample.dtype}, shape={sample.shape}")
        print(f"  - timestep:              dtype={timestep.dtype}, shape={timestep.shape}")
        print(f"  - encoder_hidden_states: dtype={encoder_hidden_states.dtype}, shape={encoder_hidden_states.shape}")
        print(f"  - text_embeds:           dtype={text_embeds.dtype}, shape={text_embeds.shape}")
        print(f"  - time_ids:              dtype={time_ids.dtype}, shape={time_ids.shape}")
        print("------------------------------------------------------------------\n")
        
        added_cond_kwargs = {"text_embeds": text_embeds, "time_ids": time_ids}
        return self.unet(
            sample,
            timestep,
            encoder_hidden_states=encoder_hidden_states,
            added_cond_kwargs=added_cond_kwargs,
        ).sample

def main():
    """
    Exports the UNet of an SDXL model to ONNX using torch.onnx.export with dynamo.
    """
    model_id = "socks22/sdxl-wai-nsfw-illustriousv14"
    output_path = "unet.onnx"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    print(f"Loading SDXL model: {model_id}")
    # Load model and force it into FP16, then use that for all tensors.
    pipe = StableDiffusionXLPipeline.from_pretrained(model_id, torch_dtype=torch.float16, use_safetensors=True)
    
    print("Loading and fusing DMD2 LoRA...")
    pipe.load_lora_weights("tianweiy/DMD2", weight_name="dmd2_sdxl_4step_lora_fp16.safetensors")
    pipe.fuse_lora(lora_scale=0.8)
    
    unet = pipe.unet
    unet.to(device)
    
    unet.eval()
    unet_dtype = unet.dtype
    print(f"UNet dtype: {unet_dtype}")

    print("Preparing dummy inputs for UNet export...")
    # SDXL uses classifier-free guidance, so inputs are duplicated (one for conditional, one for unconditional)
    # With DMD2, we can use CFG 1.0, so no duplication is needed.
    batch_size = 1

    # These are latent space dimensions, not image dimensions.
    # The default for SDXL is 1024x1024, which corresponds to 128x128 in latent space.
    latent_height = 1024 // 8
    latent_width = 1024 // 8

    # Get model-specific dimensions
    unet_in_channels = unet.config.in_channels
    unet_latent_shape = (batch_size, unet_in_channels, latent_height, latent_width)
    
    # SDXL has two text encoders, their embeddings are concatenated.
    # Text encoder 1: 768, Text encoder 2: 1280.
    # The UNet expects the concatenated projection of 2048.
    text_embed_dim = 2048 
    encoder_hidden_states_shape = (batch_size, 77, text_embed_dim)

    # Additional conditioning from the second text encoder's pooled output
    add_text_embeds_shape = (batch_size, 1280)

    # Additional conditioning for image size and cropping
    add_time_ids_shape = (batch_size, 6)

    # Create dummy tensors with the dtypes discovered from the pipeline test
    sample = torch.randn(unet_latent_shape, dtype=unet_dtype).to(device)
    timestep = torch.tensor(999, dtype=torch.float32).to(device)
    encoder_hidden_states = torch.randn(encoder_hidden_states_shape, dtype=unet_dtype).to(device)
    text_embeds = torch.randn(add_text_embeds_shape, dtype=unet_dtype).to(device)
    time_ids = torch.randn(add_time_ids_shape, dtype=unet_dtype).to(device)

    model_args = (sample, timestep, encoder_hidden_states, text_embeds, time_ids)

    print("Wrapping UNet for ONNX export.")
    unet_wrapper = UNetWrapper(unet)
    
    print("Exporting UNet to ONNX with TorchDynamo...")

    # Define dynamic axes for the model inputs. This is the new way to specify
    # dynamic shapes for the dynamo exporter.
    # Re-using the same Dim object tells the exporter that these dimensions
    # are constrained to be the same.
    
    dynamic_shapes = {
        "sample": {
            0: Dim("batch_size"),
            2: Dim("height"),
            3: Dim("width"),
        },
        "timestep": {},
        "encoder_hidden_states": {0: Dim("batch_size"), 1: Dim("num_tokens")},
        "text_embeds": {0: Dim("batch_size")},
        "time_ids": {0: Dim("batch_size")},
    }

    onnx_program = torch.onnx.export(
        unet_wrapper,
        model_args,
        dynamo=True,
        dynamic_shapes=dynamic_shapes,
        opset_version=18,
    )

    print("Optimizing ONNX model...")
    onnx_program.optimize()

    print("\n--- ONNX Model Inputs ---")
    for i, input_proto in enumerate(onnx_program.model_proto.graph.input):
        print(f"{i}: {input_proto.name}")

    print("\n--- ONNX Model Outputs ---")
    for i, output_proto in enumerate(onnx_program.model_proto.graph.output):
        print(f"{i}: {output_proto.name}\n")

    print(f"Saving ONNX model to {output_path}...")
    # The new ONNXProgram object has a save method.
    onnx_program.save(output_path)

    print(f"UNet successfully exported to {output_path}")

if __name__ == "__main__":
    main() 