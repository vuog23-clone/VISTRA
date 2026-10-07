from pathlib import Path
import sys
from typing import List
import torch
from transformers import AutoConfig, AutoModel, AutoProcessor
from transformers.utils import SAFE_WEIGHTS_NAME, cached_file
from safetensors import safe_open
from PIL import Image
import torch.nn.functional as F

from src.utils.utils import load_rgb_image, load_config, resolve_path

class ImageEmbedder:
    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        normalize: bool | None = None,
        cache_dir: str | Path | None = None,
     ):
        self.config = load_config()
        self.normalize = normalize if normalize is not None else self.config["image_normalize"]
        
        device = device if device is not None else self.config["embedding_device"]
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else torch.device(device)

        self.dtype = torch.float16 if self.device.type == "cuda" else torch.float32
        print(f"Device: {self.device}")

        self.processor = AutoProcessor.from_pretrained(
            model_name or self.config["image_embedding_model"],
            cache_dir=str(resolve_path(cache_dir or self.config["cache_folder"])),
            local_files_only=self.config["models_local_files_only"],
            )

        model_name = model_name or self.config["image_embedding_model"]
        load_options = {
            "cache_dir": str(resolve_path(cache_dir or self.config["cache_folder"])),
            "local_files_only": self.config["models_local_files_only"],
        }
        if sys.platform == "win32":
            # Avoid the safetensors mmap path that crashes natively on Windows.
            weights_path = cached_file(model_name, SAFE_WEIGHTS_NAME, **load_options)
            with torch.device(self.device):
                self.model = AutoModel.from_config(
                    AutoConfig.from_pretrained(model_name, **load_options), dtype=self.dtype
                )
            state_dict = {}
            with safe_open(weights_path, framework="pt", backend="pread") as weights:
                for name in weights.keys():
                    tensor = weights.get_tensor(name)
                    state_dict[name] = tensor.to(
                        device=self.device,
                        dtype=self.dtype if tensor.is_floating_point() else tensor.dtype,
                    )
                    del tensor
            self.model.load_state_dict(state_dict, assign=True)
            del state_dict
        else:
            self.model = AutoModel.from_pretrained(model_name, dtype=self.dtype, **load_options)
        
        self.model = self.model.to(device=self.device, dtype=self.dtype).eval()


    _load_image = staticmethod(load_rgb_image)


    @staticmethod
    def _get_embeddings(features: object) -> torch.Tensor:
        if isinstance(features, torch.Tensor):
            return features

        return features.pooler_output


    def _normalize(
        self,
        embeddings: torch.Tensor,
    ) -> torch.Tensor:

        if self.normalize:
            embeddings = F.normalize(
                embeddings,
                p=2,
                dim=-1
            )

        return embeddings


    @torch.inference_mode()
    def embed_image(
        self,
        image: str | Path | Image.Image,
    ) -> torch.Tensor:

        image = self._load_image(image)

        inputs = self.processor(
            images=image,
            return_tensors="pt",
        )

        inputs = {
            key: value.to(
                device=self.device,
                dtype=self.dtype if value.is_floating_point() else value.dtype,
            )
            for key, value in inputs.items()
        }

        embeddings = self._get_embeddings(self.model.get_image_features(**inputs))
        embeddings = self._normalize(embeddings)
        embeddings = embeddings.squeeze(0)

        return embeddings.cpu()


    @torch.inference_mode()
    def embed_batch(
            self,
            images: List[str | Path | Image.Image],
    ) -> torch.Tensor:

        loaded_images = [self._load_image(image) for image in images]

        inputs = self.processor(
            images=loaded_images,
            return_tensors="pt"
            )
        
        inputs = {
            key: value.to(
                device=self.device,
                dtype=self.dtype if value.is_floating_point() else value.dtype,
            )
            for key, value in inputs.items()
        }

        embeddings = self._get_embeddings(self.model.get_image_features(**inputs))
        embeddings = self._normalize(embeddings)

        return embeddings.cpu()


    @torch.inference_mode()
    def embed_text(
        self,
        text: str | List[str],
    ) -> torch.Tensor:
        
        if isinstance(text, str):
            text = [text]
        
        inputs = self.processor(
            text=text,
            padding="max_length",
            truncation=True,
            max_length=self.model.config.text_config.max_position_embeddings,
            return_tensors="pt",
        )

        inputs = {
            key: value.to(
                device=self.device,
                dtype=self.dtype if value.is_floating_point() else value.dtype,
            )
            for key, value in inputs.items()
        }

        embeddings = self._get_embeddings(self.model.get_text_features(**inputs))
        embeddings = self._normalize(embeddings)

        return embeddings.cpu()


    def embed_numpy(
        self,
        image: str | Path | Image.Image,
    ):
        return self.embed_image(image).numpy()


    def embed_batch_numpy(
        self,
        images: List[str | Path | Image.Image],
    ):
        return self.embed_batch(images).numpy()
