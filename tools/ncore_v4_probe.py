#!/usr/bin/env python3
"""Generate and load a minimal multimodal NCore V4 sequence.

This probe intentionally runs only in the disposable environment documented by
Decision 0002.  NCore is not a CartoSentry V1 dependency.
"""

from __future__ import annotations

import io
import json
import platform
import tempfile
from importlib.metadata import version

import numpy as np
from ncore.data import (
    FrameTimepoint,
    IdealPinholeCameraModelParameters,
    RowOffsetStructuredSpinningLidarModelParameters,
    ShutterType,
)
from ncore.data.v4 import (
    CameraSensorComponent,
    IntrinsicsComponent,
    LidarSensorComponent,
    PosesComponent,
    RadarSensorComponent,
    SequenceComponentGroupsReader,
    SequenceComponentGroupsWriter,
    SequenceLoaderV4,
)
from ncore.impl.common.transformations import HalfClosedInterval
from PIL import Image
from upath import UPath


def _normalize(vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    norms = np.linalg.norm(vectors, axis=1).astype(np.float32)
    return (vectors / norms[:, None]).astype(np.float32), norms


def _package_versions() -> dict[str, str]:
    names = (
        "cbor2",
        "dataclasses-json",
        "numpy",
        "nvidia-ncore",
        "pillow",
        "scipy",
        "torch",
        "typing-extensions",
        "universal-pathlib",
        "zarr",
    )
    return {name: version(name) for name in names}


def main() -> None:
    start = 1_000_000
    stop = 2_000_001
    camera_id = "camera-front"
    lidar_id = "lidar-top"
    radar_id = "radar-front"

    with tempfile.TemporaryDirectory(prefix="cartosentry-ncore-v4-") as temp:
        writer = SequenceComponentGroupsWriter(
            output_dir_path=UPath(temp),
            store_base_name="cartosentry-generated",
            sequence_id="cartosentry-generated",
            sequence_timestamp_interval_us=HalfClosedInterval(start, stop),
            store_type="directory",
            generic_meta_data={"fixture": "generated", "external_bytes": False},
        )

        transforms = np.repeat(np.eye(4, dtype=np.float64)[None, :, :], 3, axis=0)
        transforms[:, 0, 3] = np.array([0.0, 1.0, 2.0])
        pose_timestamps = np.array([start, 1_500_000, stop - 1], dtype=np.uint64)
        poses = writer.register_component_writer(
            PosesComponent.Writer, "default", group_name=None
        )
        poses.store_dynamic_pose("rig", "world", transforms, pose_timestamps)
        poses.store_static_pose("world", "world_global", np.eye(4, dtype=np.float64))
        for sensor_id, x_offset in (
            (camera_id, 0.1),
            (lidar_id, 0.2),
            (radar_id, 0.3),
        ):
            extrinsic = np.eye(4, dtype=np.float32)
            extrinsic[0, 3] = x_offset
            poses.store_static_pose(sensor_id, "rig", extrinsic)

        intrinsics = writer.register_component_writer(
            IntrinsicsComponent.Writer, "default", "intrinsics"
        )
        intrinsics.store_camera_intrinsics(
            camera_id,
            IdealPinholeCameraModelParameters(
                resolution=np.array([2, 2], dtype=np.uint64),
                shutter_type=ShutterType.GLOBAL,
                principal_point=np.array([0.5, 0.5], dtype=np.float32),
                focal_length=np.array([1.0, 1.0], dtype=np.float32),
            ),
        )
        intrinsics.store_lidar_intrinsics(
            lidar_id,
            RowOffsetStructuredSpinningLidarModelParameters(
                spinning_frequency_hz=10.0,
                spinning_direction="ccw",
                n_rows=2,
                n_columns=2,
                row_elevations_rad=np.array([0.1, -0.1], dtype=np.float32),
                column_azimuths_rad=np.array([-1.0, 1.0], dtype=np.float32),
                row_azimuth_offsets_rad=np.zeros(2, dtype=np.float32),
            ),
        )

        camera = writer.register_component_writer(
            CameraSensorComponent.Writer, camera_id, "cameras"
        )
        image_buffer = io.BytesIO()
        Image.fromarray(
            np.array(
                [
                    [[255, 0, 0], [0, 255, 0]],
                    [[0, 0, 255], [255, 255, 255]],
                ],
                dtype=np.uint8,
            )
        ).save(image_buffer, format="PNG")
        camera.store_frame(
            image_buffer.getvalue(),
            "png",
            np.array([start, start + 100_000], dtype=np.uint64),
            generic_data={},
            generic_meta_data={"frame": 0},
        )

        lidar = writer.register_component_writer(
            LidarSensorComponent.Writer, lidar_id, "lidars"
        )
        lidar_directions, lidar_distances = _normalize(
            np.array(
                [[1.0, 0.1, 0.0], [1.0, 0.0, 0.1], [1.0, -0.1, 0.0]],
                dtype=np.float32,
            )
        )
        lidar.store_frame(
            lidar_directions,
            np.array([start, start + 50_000, start + 100_000], dtype=np.uint64),
            np.array([[0, 0], [0, 1], [1, 0]], dtype=np.uint16),
            lidar_distances[None, :],
            np.array([[0.2, 0.5, 0.8]], dtype=np.float32),
            np.array([start, start + 100_000], dtype=np.uint64),
            generic_data={},
            generic_meta_data={"frame": 0},
        )

        radar = writer.register_component_writer(
            RadarSensorComponent.Writer, radar_id, "radars"
        )
        radar_directions, radar_distances = _normalize(
            np.array([[1.0, 0.2, 0.0], [1.0, -0.2, 0.0]], dtype=np.float32)
        )
        radar.store_frame(
            radar_directions,
            np.array([start + 25_000, start + 25_000], dtype=np.uint64),
            radar_distances[None, :],
            np.array([start + 25_000, start + 25_000], dtype=np.uint64),
            generic_data={
                "velocity_mps": np.array(
                    [[1.0, 0.0, 0.0], [0.5, 0.0, 0.0]], dtype=np.float32
                )
            },
            generic_meta_data={"frame": 0},
        )

        paths = writer.finalize()
        loader = SequenceLoaderV4(
            SequenceComponentGroupsReader(paths, open_consolidated=False),
            masks_component_group_name=None,
            cuboids_component_group_name=None,
        )

        camera_sensor = loader.get_camera_sensor(camera_id)
        lidar_sensor = loader.get_lidar_sensor(lidar_id)
        radar_sensor = loader.get_radar_sensor(radar_id)
        evaluated_pose = loader.pose_graph.evaluate_poses(
            "rig", "world", np.array([1_500_000], dtype=np.uint64)
        )

        result = {
            "environment": {
                "machine": platform.machine(),
                "packages": _package_versions(),
                "platform_system": platform.system(),
                "python": platform.python_version(),
            },
            "fixture": {
                "external_bytes": False,
                "kind": "deterministic-generated-v4",
                "sequence_id": loader.sequence_id,
                "store_type": "directory",
            },
            "observed": {
                "camera_extrinsic_shape": list(camera_sensor.T_sensor_rig.shape),
                "camera_ids": loader.camera_ids,
                "camera_image_shape": list(
                    camera_sensor.get_frame_image_array(0).shape
                ),
                "camera_model": type(camera_sensor.model_parameters).__name__,
                "camera_timestamp_us": int(
                    camera_sensor.get_frame_timestamp_us(0, FrameTimepoint.START)
                ),
                "lidar_distance_m": lidar_sensor.get_frame_ray_bundle_return_distance_m(
                    0
                ).tolist(),
                "lidar_extrinsic_shape": list(lidar_sensor.T_sensor_rig.shape),
                "lidar_ids": loader.lidar_ids,
                "lidar_intensity": lidar_sensor.get_frame_ray_bundle_return_intensity(
                    0
                ).tolist(),
                "lidar_model": type(lidar_sensor.model_parameters).__name__,
                "lidar_timestamp_us": lidar_sensor.get_frame_ray_bundle_timestamp_us(
                    0
                ).tolist(),
                "pose_shape": list(evaluated_pose.shape),
                "radar_distance_m": radar_sensor.get_frame_ray_bundle_return_distance_m(
                    0
                ).tolist(),
                "radar_extrinsic_shape": list(radar_sensor.T_sensor_rig.shape),
                "radar_ids": loader.radar_ids,
                "radar_timestamp_us": radar_sensor.get_frame_ray_bundle_timestamp_us(
                    0
                ).tolist(),
                "timestamp_interval_us": [
                    loader.sequence_timestamp_interval_us.start,
                    loader.sequence_timestamp_interval_us.stop,
                ],
            },
        }
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
