"""Concrete camera and detector backends (optional deps: `pip install jarvis[vision]`)."""

from __future__ import annotations

import base64

import numpy as np

from .base import Detection


class OpenCVCamera:
    """A webcam or video stream (device index, file path, or RTSP/HTTP URL)."""

    def __init__(self, source: int | str = 0) -> None:
        import cv2

        self._cv2 = cv2
        self._cap = cv2.VideoCapture(source)
        if not self._cap.isOpened():
            raise RuntimeError(f"cannot open camera {source!r}")

    def read(self) -> np.ndarray | None:
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        self._cap.release()


class YoloDetector:
    """Ultralytics YOLO. Point `weights` at your own trained model (e.g. from the CCTV project)."""

    def __init__(self, weights: str = "yolo11n.pt", min_confidence: float = 0.4) -> None:
        from ultralytics import YOLO

        self._model = YOLO(weights)
        self.min_confidence = min_confidence

    def detect(self, frame: np.ndarray) -> list[Detection]:
        result = self._model(frame, verbose=False, conf=self.min_confidence)[0]
        boxes = result.boxes
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        classes = boxes.cls.cpu().numpy().astype(int)
        return [
            Detection(result.names[int(c)], float(p), tuple(float(v) for v in b))
            for b, p, c in zip(xyxy, confs, classes)
        ]


def encode_jpeg(frame: np.ndarray, max_side: int = 1280, quality: int = 80) -> str:
    """Downscale and JPEG-encode a frame, returning base64 for a Claude image block."""
    import cv2

    h, w = frame.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")
