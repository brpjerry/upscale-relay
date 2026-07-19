"""ONNX model manifest loading and default generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_SCALE_MARKER = re.compile(
    r"(?<!\d)(?:(?P<number_first>[1-9]\d*)x|x(?P<x_first>[1-9]\d*))(?!\d)",
    re.IGNORECASE,
)


def infer_scale_factor(model_path: str | Path) -> int:
    """Infer a scale marker such as ``3x`` or ``x4`` from a model name.

    Resolution-like strings (for example, ``1080x1920``) and convolution
    labels such as ``3x3`` are intentionally ignored. Models without a scale
    marker default to the project's most common scale, 2x.
    """
    match = _SCALE_MARKER.search(Path(model_path).stem)
    if match is None:
        return 2
    return int(match.group("number_first") or match.group("x_first"))


@dataclass
class ModelManifest:
    scale_factor: int | None = None
    channel_order: str = "rgb"
    value_range: tuple[float, float] = (0.0, 1.0)

    @classmethod
    def generated_for(cls, model_path: str | Path) -> "ModelManifest":
        return cls(scale_factor=infer_scale_factor(model_path))

    @classmethod
    def load(cls, model_path: str | Path) -> "ModelManifest":
        """Load a sidecar manifest, creating default metadata if absent.

        A read-only models directory does not prevent use of the inferred
        defaults; it only prevents the generated sidecar from being persisted.
        Existing manifests are never modified or replaced.
        """
        manifest_path = Path(model_path).with_suffix(".json")
        if manifest_path.exists():
            return cls._read(manifest_path)

        manifest = cls.generated_for(model_path)
        try:
            # Exclusive creation prevents concurrent server/worker startup from
            # replacing a sidecar that another process just wrote.
            with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest.as_dict(), stream, indent=2)
                stream.write("\n")
        except FileExistsError:
            return cls._read(manifest_path)
        except OSError:
            pass
        return manifest

    @classmethod
    def _read(cls, manifest_path: Path) -> "ModelManifest":
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        return cls(
            scale_factor=data.get("scale_factor"),
            channel_order=data.get("channel_order", "rgb"),
            value_range=tuple(data.get("value_range", (0.0, 1.0))),
        )

    def as_dict(self) -> dict:
        return {
            "scale_factor": self.scale_factor,
            "channel_order": self.channel_order,
            "value_range": list(self.value_range),
        }
