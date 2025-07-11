import onnxruntime as ort
import numpy as np
import torch
from dataclasses import dataclass
from typing import Optional, Tuple
import os
from transformers import CLIPTextModel, CLIPTextModelWithProjection


@dataclass
class ONNXCLIPTextOutput:
    last_hidden_state: torch.Tensor
    pooler_output: Optional[torch.Tensor] = None
    hidden_states: Optional[Tuple[torch.Tensor]] = None
    text_embeds: Optional[torch.Tensor] = None

class ONNXModel:
    def __init__(self, model_path: str, device: torch.device):
        self.device = device
        subfolder = os.path.basename(os.path.dirname(model_path))
        filename = os.path.basename(model_path)
        print(f"\\n--- Creating InferenceSession for: {subfolder}/{filename} ---")
        #so = ort.SessionOptions()
        #so.log_severity_level = 1
        provider_options = [{"device_id": self.device.index}]
        self.session = ort.InferenceSession(
            model_path, providers=[("CUDAExecutionProvider")]
        )
        self.io_binding = self.session.io_binding()
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.output_names = [o.name for o in self.session.get_outputs()]

    def bind_input(self, name: str, tensor: torch.Tensor):
        tensor = tensor.contiguous()
        self.io_binding.bind_input(
            name=name,
            device_type='cuda',
            device_id=self.device.index,
            element_type=np.float16 if tensor.dtype == torch.float16 else np.float32 if tensor.dtype == torch.float32 else np.int64,
            shape=tensor.shape,
            buffer_ptr=tensor.data_ptr(),
        )

    def bind_output(self, name: str, tensor: torch.Tensor):
        tensor = tensor.contiguous()
        self.io_binding.bind_output(
            name=name,
            device_type='cuda',
            device_id=self.device.index,
            element_type=np.float16 if tensor.dtype == torch.float16 else np.float32 if tensor.dtype == torch.float32 else np.int64,
            shape=tensor.shape,
            buffer_ptr=tensor.data_ptr(),
        )

class VAEDecoder(ONNXModel):
    def __init__(self, model_path: str, device: torch.device):
        super().__init__(model_path, device)

    def __call__(self, latent: torch.Tensor) -> torch.Tensor:
        self.io_binding.clear_binding_inputs()
        self.io_binding.clear_binding_outputs()
        
        print("--- VAEDecoder Input ---")
        print(f"latent: shape={latent.shape}, dtype={latent.dtype}, device={latent.device}")
        print(f"latent | Mean: {latent.mean():.6f} | Std: {latent.std():.6f} | Sum: {latent.sum():.6f}")
        print("------------------------")

        self.bind_input("latent_sample", latent.to(torch.float16))
        
        output_shape = (latent.shape[0], 3, latent.shape[2] * 8, latent.shape[3] * 8)
        output_tensor = torch.empty(output_shape, dtype=torch.float16, device=self.device)
        self.bind_output("sample", output_tensor)
        
        self.session.run_with_iobinding(self.io_binding)
        return output_tensor


class UNet(ONNXModel):
    def __init__(self, model_path: str, device: torch.device):
        super().__init__(model_path, device)

    def __call__(self, latent: torch.Tensor, timestep: torch.Tensor, text_embedding: torch.Tensor, text_embeds: torch.Tensor, time_ids: torch.Tensor) -> torch.Tensor:
        self.io_binding.clear_binding_inputs()
        self.io_binding.clear_binding_outputs()

        latent = latent.to(torch.float16)
        timestep = timestep.to(torch.float16)
        text_embedding = text_embedding.to(torch.float16)
        text_embeds = text_embeds.to(torch.float16)
        time_ids = time_ids.to(torch.float16)

        print("--- UNet Inputs ---")
        print(f"latent: shape={latent.shape}, dtype={latent.dtype}, device={latent.device}")
        print(f"latent | Mean: {latent.mean():.6f} | Std: {latent.std():.6f} | Sum: {latent.sum():.6f}")
        print(f"timestep: shape={timestep.shape}, dtype={timestep.dtype}, device={timestep.device}, value: {timestep.item()}")
        print(f"text_embedding: shape={text_embedding.shape}, dtype={text_embedding.dtype}, device={text_embedding.device}")
        print(f"text_embedding | Mean: {text_embedding.mean():.6f} | Std: {text_embedding.std():.6f} | Sum: {text_embedding.sum():.6f}")
        print(f"text_embeds: shape={text_embeds.shape}, dtype={text_embeds.dtype}, device={text_embeds.device}")
        print(f"text_embeds | Mean: {text_embeds.mean():.6f} | Std: {text_embeds.std():.6f} | Sum: {text_embeds.sum():.6f}")
        print(f"time_ids: shape={time_ids.shape}, dtype={time_ids.dtype}, device={time_ids.device}")
        print(f"time_ids | Mean: {time_ids.mean():.6f} | Std: {time_ids.std():.6f} | Sum: {time_ids.sum():.6f}")
        print("--------------------")

        self.bind_input("sample", latent)
        self.bind_input("timestep", timestep)
        self.bind_input("encoder_hidden_states", text_embedding)
        self.bind_input("text_embeds", text_embeds)
        self.bind_input("time_ids", time_ids)

        output_tensor = torch.empty(latent.shape, dtype=latent.dtype, device=self.device)
        self.bind_output("out_sample", output_tensor)
        
        self.session.run_with_iobinding(self.io_binding)
        return output_tensor


class CLIPTextEncoder(ONNXModel):
    def __init__(self, model_path: str, device: torch.device, name: str = "CLIPTextEncoder"):
        super().__init__(model_path, device)
        self.name = name
        self.hidden_size = None
        self.pooler_dim = None

        if self.name == "CLIP-L":
            self.last_hidden_state_name = "hidden_states.11"
            self.pooler_output_name = None
        elif self.name == "CLIP-G":
            self.last_hidden_state_name = "hidden_states.31"
            self.pooler_output_name = "text_embeds"
        else:
            raise ValueError(f"Unknown CLIP model name: {self.name}")

        for output in self.session.get_outputs():
            if output.name == self.last_hidden_state_name:
                self.hidden_size = output.shape[-1]
            elif self.pooler_output_name and output.name == self.pooler_output_name:
                self.pooler_dim = output.shape[-1]

    def __call__(
        self,
        input_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ):
        self.io_binding.clear_binding_inputs()
        self.io_binding.clear_binding_outputs()

        input_ids = input_ids.to(torch.int64)

        print(f"--- {self.name} ONNX Input ---")
        print(f"input_ids: shape={input_ids.shape}, dtype={input_ids.dtype}, device={input_ids.device}")
        print(f"tokens: {input_ids.flatten().tolist()}")
        if attention_mask is not None:
            print(f"attention_mask: shape={attention_mask.shape}, dtype={attention_mask.dtype}, device={attention_mask.device}")
        if output_hidden_states is not None:
            print(f"output_hidden_states: {output_hidden_states}")
        print("---------------------------")

        self.bind_input("input_ids", input_ids)

        # Prepare output tensors
        batch_size, seq_len = input_ids.shape
        
        print(f"--- {self.name} Prepared Output Shapes ---")
        last_hidden_state_shape = (batch_size, seq_len, self.hidden_size)
        print(f"last_hidden_state_shape: {last_hidden_state_shape}")
        last_hidden_state = torch.empty(last_hidden_state_shape, dtype=torch.float16, device=self.device)
        self.bind_output(self.last_hidden_state_name, last_hidden_state)
        
        pooler_output = None
        if self.name == "CLIP-G":
            pooler_output_shape = (batch_size, self.pooler_dim)
            print(f"pooler_output_shape: {pooler_output_shape}")
            pooler_output = torch.empty(pooler_output_shape, dtype=torch.float16, device=self.device)
            self.bind_output(self.pooler_output_name, pooler_output)
        print("------------------------------------")

        self.session.run_with_iobinding(self.io_binding)

        print(f"--- {self.name} ONNX Output ---")
        
        last_hidden_state_nan_count = torch.isnan(last_hidden_state).sum()
        pooler_output_nan_count = torch.isnan(pooler_output).sum() if pooler_output is not None else 0
        
        print(f"{self.last_hidden_state_name}: shape={last_hidden_state.shape}, dtype={last_hidden_state.dtype}, device={last_hidden_state.device}, nans={last_hidden_state_nan_count}/{last_hidden_state.numel()}")
        print(f"{self.last_hidden_state_name} | Mean: {last_hidden_state.mean():.6f} | Std: {last_hidden_state.std():.6f} | Sum: {last_hidden_state.sum():.6f}")
        if pooler_output is not None:
            print(f"{self.pooler_output_name}: shape={pooler_output.shape}, dtype={pooler_output.dtype}, device={pooler_output.device}, nans={pooler_output_nan_count}/{pooler_output.numel()}")
            print(f"{self.pooler_output_name} | Mean: {pooler_output.mean():.6f} | Std: {pooler_output.std():.6f} | Sum: {pooler_output.sum():.6f}")
        print("----------------------------")

        hidden_states = None
        if output_hidden_states:
            hidden_states = (last_hidden_state,)

        return ONNXCLIPTextOutput(
            last_hidden_state=last_hidden_state,
            pooler_output=pooler_output,
            hidden_states=hidden_states,
            text_embeds=pooler_output,
        )

class WDTaggerONNX(ONNXModel):
    def __init__(self, model_path: str, device: torch.device):
        super().__init__(model_path, device)
        self.input_shape = self.session.get_inputs()[0].shape
        self.image_size = self.input_shape[2]

    def __call__(self, image: torch.Tensor) -> torch.Tensor:
        self.io_binding.clear_binding_inputs()
        self.io_binding.clear_binding_outputs()

        self.bind_input(self.input_names[0], image.contiguous())
        
        output_tensor = torch.empty(self.session.get_outputs()[0].shape, dtype=torch.float32, device=self.device)
        self.bind_output(self.output_names[0], output_tensor)
        
        self.session.run_with_iobinding(self.io_binding)
        return output_tensor