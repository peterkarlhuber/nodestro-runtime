"""Names and size limits of Nodestro's environment images (``nodestro-env:<key>``).

Shared by :mod:`nodestro_runtime.container_images` (which builds the images and collects unused ones) and
:mod:`nodestro_runtime.sandbox_gateway` (which refuses builds beyond its limits). This module imports nothing, so the
gateway stays free of the runtime's dependencies.
"""

from __future__ import annotations

#: The repository of environment images (the tag is the image key).
IMAGE_REPOSITORY = "nodestro-env"
#: The label that names the lock an image was built from. Only images that carry it are ever removed by Nodestro.
LOCK_LABEL = "org.nodestro.lock-sha256"
GIB = 1024**3

#: :meth:`~nodestro_runtime.container_images.ContainerImages.gc` keeps at most this many environment images, and at
#: most this many GiB of them. Both stay below the sandbox gateway's limits, so a desktop that collects its images
#: never reaches the gateway's refusal (507).
GC_MAX_IMAGES = 20
GC_MAX_GB = 20.0
#: The sandbox gateway refuses a new build beyond these (``NODESTRO_SANDBOX_MAX_IMAGES`` / ``_MAX_IMAGES_GB``).
GATEWAY_MAX_IMAGES = 32
GATEWAY_MAX_IMAGES_GB = 20.0
#: A single environment image larger than this (GiB, ``NODESTRO_MAX_IMAGE_GB``) is removed right after its build and
#: the build fails (``deps.image_too_large``).
MAX_IMAGE_GB = 8.0
MAX_IMAGE_GB_VARIABLE = "NODESTRO_MAX_IMAGE_GB"
#: The sandbox gateway's refusal (429) of a build while another one runs. A build that fails with it is not broken: the
#: Docker sandbox's self-test waits for the other build and tries again (:mod:`nodestro_runtime.docker_status`).
BUILD_BUSY = "Another image build is running; try again when it is done"

__all__ = [
    "BUILD_BUSY",
    "GATEWAY_MAX_IMAGES",
    "GATEWAY_MAX_IMAGES_GB",
    "GC_MAX_GB",
    "GC_MAX_IMAGES",
    "GIB",
    "IMAGE_REPOSITORY",
    "LOCK_LABEL",
    "MAX_IMAGE_GB",
    "MAX_IMAGE_GB_VARIABLE",
]
