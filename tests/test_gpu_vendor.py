# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0


def builds(service):
    """Count the queued base image builds.

    Args:
        service: The service.

    Returns:
        The number of queued builds.
    """
    return service.db.scalar("SELECT count(*) FROM builds WHERE status = 'queued'")


def test_base_build_gets_the_gpu_vendor(service):
    """Base images are built with the GPU setting, so Intel images get the compute runtime."""
    service.db.set_setting("gpu_vendor", "intel")
    service.client.containers.run.return_value = b"bash\n"
    service.builder._build_base(service.builder.queue_base(1), 1)
    assert service.client.api.build.call_args.kwargs["buildargs"]["GPU_VENDOR"] == "intel"


def test_changing_the_vendor_rebuilds_every_image(service):
    """A new GPU vendor queues a rebuild of each image; saving the same vendor does not."""
    service.add_terminal_image("boss", "baseimage-ubuntu:noble")
    service.db.execute("DELETE FROM builds")
    service.db.execute("UPDATE images SET status = 'ready'")
    images = service.db.scalar("SELECT count(*) FROM images")
    service.update_settings("boss", {"gpu_vendor": "intel"})
    assert builds(service) == images
    service.update_settings("boss", {"gpu_vendor": "intel"})
    assert builds(service) == images
    assert service.db.scalar("SELECT count(*) FROM audit WHERE action = 'image.rebuild'") == images
