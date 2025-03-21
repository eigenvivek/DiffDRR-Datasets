# AUTOGENERATED! DO NOT EDIT! File to edit: ../notebooks/00_deepfluoro.ipynb.

# %% auto 0
__all__ = ['DeepFluoroDataset', 'preprocess', 'Transforms']

# %% ../notebooks/00_deepfluoro.ipynb 3
from pathlib import Path

import h5py
import numpy as np
import torch
from .utils import load_file
from torchio import LabelMap, ScalarImage, Subject
from torchio.transforms.preprocessing import ToCanonical
from torchvision.transforms.functional import center_crop, gaussian_blur

from diffdrr.data import read
from diffdrr.detector import parse_intrinsic_matrix
from diffdrr.pose import RigidTransform

# %% ../notebooks/00_deepfluoro.ipynb 6
class DeepFluoroDataset(torch.utils.data.Dataset):
    """
    A `torch.utils.data.Dataset` that stores the imaging data for subjects
    in the `DeepFluoro` dataset and provides an iterator over the X-ray
    fluoroscopy images and associated poses for each subject. Imaging data
    can be passed to a `diffdrr.drr.DRR` to renderer DRRs from ground truth
    camera poses.
    """

    def __init__(
        self,
        id_number: int,  # Subject ID in {1, ..., 6}
        preprocess: bool = True,  # Convert X-rays from exponentiated to linear form
        bone_attenuation_multiplier: float = 1.0,  # Scalar multiplier on density of high attenuation voxels (from `DiffDRR`, see [here](https://vivekg.dev/DiffDRR/tutorials/introduction.html#changing-the-appearance-of-the-rendered-drrs))
        labels: int | list = None,  # Labels from the mask of structures to render
        batchless: bool = False,  # Return unbatched images and poses (e.g., to interface with a `torch.utils.data.DataLoader`)
    ):
        super().__init__()

        # Load the subject
        (
            self.subject,
            self.projections,
            self.anatomical2world,
            self.world2camera,
            self.focal_len,
            self.height,
            self.width,
            self.delx,
            self.dely,
            self.x0,
            self.y0,
        ) = load(id_number, bone_attenuation_multiplier, labels)

        self.preprocess = preprocess
        if self.preprocess:
            self.height -= 100
            self.width -= 100
        self.batchless = batchless

        # Miscellaneous transformation matrices for wrangling SE(3) poses
        self.flip_z = RigidTransform(
            torch.tensor(
                [
                    [-1, 0, 0, 0],
                    [0, -1, 0, 0],
                    [0, 0, -1, 0],
                    [0, 0, 0, 1],
                ]
            ).to(torch.float32)
        )
        self.rot_180 = RigidTransform(
            torch.tensor(
                [
                    [-1, 0, 0, 0],
                    [0, -1, 0, 0],
                    [0, 0, 1, 0],
                    [0, 0, 0, 1],
                ]
            ).to(torch.float32)
        )
        self.reorient = RigidTransform(self.subject.reorient)

    def __len__(self):
        return len(self.projections)

    def __iter__(self):
        return iter(self[idx] for idx in range(len(self)))

    def __getitem__(self, idx):
        img = torch.from_numpy(self.projections[f"{idx:03d}/image/pixels"][:])
        pose = self.projections[f"{idx:03d}/gt-poses/cam-to-pelvis-vol"][:]
        pose = RigidTransform(torch.from_numpy(pose))
        pose = (
            self.flip_z
            .compose(self.world2camera.inverse())
            .compose(pose)
            .compose(self.anatomical2world)
            .compose(self.rot_180)
        )
        if self.rot_180_for_up(idx):
            img = torch.rot90(img, k=2)
            pose = self.rot_180.compose(pose)
        pose = self.reorient.inverse().compose(pose)
        img = img.unsqueeze(0).unsqueeze(0)
        if self.preprocess:
            img = preprocess(img)
        if self.batchless:
            return img[0], pose.matrix[0]
        else:
            return img, pose

    def rot_180_for_up(self, idx):
        return self.projections[f"{idx:03d}/rot-180-for-up"][()]

# %% ../notebooks/00_deepfluoro.ipynb 8
def parse_volume(subject, bone_attenuation_multiplier, labels):
    # Get all parts of the volume
    volume = subject["vol/pixels"][:]
    volume = np.swapaxes(volume, 0, 2).copy()
    volume = torch.from_numpy(volume).unsqueeze(0).flip(1).flip(2)

    mask = subject["vol-seg/image/pixels"][:]
    mask = np.swapaxes(mask, 0, 2).copy()
    mask = torch.from_numpy(mask).unsqueeze(0).flip(1).flip(2)

    affine = np.eye(4)
    affine[:3, :3] = subject["vol/dir-mat"][:]
    affine[:3, 3:] = subject["vol/origin"][:]
    affine = torch.from_numpy(affine).to(torch.float32)

    defns = subject["vol-seg/labels-def"]
    defns = {idx: defns[f"{idx}"][()].decode() for idx in range(1, len(defns) + 1)}

    fiducials = torch.stack(
        [
            torch.from_numpy(subject[f"vol-landmarks/{key}"][()])
            for key in subject["vol-landmarks"].keys()
        ]
    ).permute(2, 0, 1)

    volume = ScalarImage(tensor=volume, affine=affine)
    labelmap = LabelMap(tensor=mask, affine=affine)

    # Move the fiducials's isocenter to the origin in world coordinates
    isocenter = volume.get_center()
    anatomical2world = RigidTransform(
        torch.tensor(
            [
                [1, 0, 0, -isocenter[0]],
                [0, 1, 0, -isocenter[1]],
                [0, 0, 1, -isocenter[2]],
                [0, 0, 0, 1],
            ],
            dtype=torch.float32,
        )
    )

    # Package the subject
    subject = read(
        volume=volume,
        labelmap=labelmap,
        labels=labels,
        orientation="PA",
        bone_attenuation_multiplier=bone_attenuation_multiplier,
        label_def=defns,
        fiducials=fiducials,
    )
    reorient = RigidTransform(torch.diag(torch.tensor([-1.0, -1.0, 1.0, 1.0])))
    subject.fiducials = reorient(subject.fiducials)

    return subject, anatomical2world


def parse_proj_params(f):
    proj_params = f["proj-params"]
    extrinsic = torch.from_numpy(proj_params["extrinsic"][:])
    camera2world = RigidTransform(extrinsic)
    intrinsic = torch.from_numpy(proj_params["intrinsic"][:])
    num_cols = proj_params["num-cols"][()]
    num_rows = proj_params["num-rows"][()]
    proj_col_spacing = float(proj_params["pixel-col-spacing"][()])
    proj_row_spacing = float(proj_params["pixel-row-spacing"][()])
    return (
        intrinsic,
        camera2world,
        num_cols,
        num_rows,
        proj_col_spacing,
        proj_row_spacing,
    )


def load(id_number, bone_attenuation_multiplier, labels):
    f = load_file("ipcai_2020_full_res_data.h5")

    # Load dataset parameters
    (
        intrinsic,
        camera2world,
        num_cols,
        num_rows,
        proj_col_spacing,
        proj_row_spacing,
    ) = parse_proj_params(f)

    focal_len, x0, y0 = parse_intrinsic_matrix(
        intrinsic,
        num_rows,
        num_cols,
        proj_row_spacing,
        proj_col_spacing,
    )

    # Load subject data
    assert id_number in {1, 2, 3, 4, 5, 6}
    subject_id = [
        "17-1882",
        "18-1109",
        "18-0725",
        "18-2799",
        "18-2800",
        "17-1905",
    ][id_number - 1]
    subject = f[subject_id]
    projections = subject["projections"]
    subject, anatomical2world = parse_volume(
        subject, bone_attenuation_multiplier, labels
    )

    return (
        subject,
        projections,
        anatomical2world,
        camera2world,
        focal_len,
        int(num_rows),
        int(num_cols),
        proj_row_spacing,
        proj_col_spacing,
        x0,
        y0,
    )

# %% ../notebooks/00_deepfluoro.ipynb 9
def preprocess(img, initial_energy=torch.tensor(65487.0)):
    """Convert X-ray fluoroscopy from the exponentiated form to the linear form."""
    img = center_crop(img, (1436, 1436))
    img = gaussian_blur(img, (5, 5), sigma=1.0)
    img = initial_energy.log() - img.log()
    return img

# %% ../notebooks/00_deepfluoro.ipynb 11
from torchvision.transforms import Compose, Lambda, Normalize, Resize


class Transforms:
    def __init__(self, height: int, eps: float = 1e-6):
        """Standardize, resize, and normalize X-rays and DRRs before inputting to a deep learning model."""
        self.transforms = Compose(
            [
                Lambda(lambda x: (x - x.min()) / (x.max() - x.min() + eps)),
                Resize((height, height), antialias=True),
                Normalize(mean=0.3080, std=0.1494),
            ]
        )

    def __call__(self, x):
        return self.transforms(x)
