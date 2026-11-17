"""crowd count from a density map (dm-count, vgg19, trained on shanghaitech part b street crowds).

yolo only finds people it can see whole, so it tops out around 10 in a dense line and even goes down as the
crowd grows (11/16 test on 100 shanghaitech b frames at 640x480: pearson -0.32 vs the true count).
dm-count on the same frames: pearson 0.97, median error 4.6 people. weights: lwcc's release (mit license).
"""
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import torch
from torch import nn

WEIGHTS_URL = "https://github.com/tersekmatija/lwcc_weights/releases/download/v0.1/DM-Count_SHB.pth"
WEIGHTS_PATH = Path(__file__).parent / "models" / "crowd" / "DM-Count_SHB.pth"
VGG19_LAYOUT = [64, 64, "M", 128, 128, "M", 256, 256, 256, 256, "M", 512, 512, 512, 512, "M", 512, 512, 512, 512]
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class DensityCounter(nn.Module):
    def __init__(self):
        super().__init__()
        layers, in_channels = [], 3
        for value in VGG19_LAYOUT:
            if value == "M":
                layers.append(nn.MaxPool2d(kernel_size=2, stride=2))
            else:
                layers += [nn.Conv2d(in_channels, value, kernel_size=3, padding=1), nn.ReLU(inplace=True)]
                in_channels = value
        self.features = nn.Sequential(*layers)
        self.reg_layer = nn.Sequential(
            nn.Conv2d(512, 256, kernel_size=3, padding=1), nn.ReLU(inplace=True),
            nn.Conv2d(256, 128, kernel_size=3, padding=1), nn.ReLU(inplace=True),
        )
        self.density_layer = nn.Sequential(nn.Conv2d(128, 1, 1), nn.ReLU())

    def forward(self, image):
        features = nn.functional.interpolate(self.features(image), scale_factor=2, mode="bilinear", align_corners=True)
        return self.density_layer(self.reg_layer(features))


def load_counter():
    if not WEIGHTS_PATH.exists():
        WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(WEIGHTS_URL, WEIGHTS_PATH)
    counter = DensityCounter()
    counter.load_state_dict(torch.load(WEIGHTS_PATH, map_location="cpu")["model"])
    return counter.eval()


def count_people(counter, frame):
    """frame: bgr uint8 from opencv. returns the estimated number of people as a float."""
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = torch.from_numpy(((rgb - IMAGENET_MEAN) / IMAGENET_STD).transpose(2, 0, 1)).unsqueeze(0)
    with torch.no_grad():
        return float(counter(tensor).sum())
