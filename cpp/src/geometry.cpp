#include "cartosentry/contracts/geometry.hpp"

#include <GeographicLib/LocalCartesian.hpp>
#include <Eigen/Core>
#include <Eigen/Geometry>
#include <sophus/se3.hpp>

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string_view>
#include <utility>

namespace cartosentry::contracts {
namespace {

auto require_finite(double value, std::string_view field) -> void {
  if (!std::isfinite(value)) {
    throw std::invalid_argument(std::string(field) + " must be finite");
  }
}

auto require_frame(std::string_view frame) -> void {
  if (frame.empty()) {
    throw std::invalid_argument("coordinate frame must be nonempty");
  }
  const auto valid = std::all_of(frame.begin(), frame.end(), [](char value) {
    return (value >= 'a' && value <= 'z') ||
           (value >= 'A' && value <= 'Z') ||
           (value >= '0' && value <= '9') || value == '_' || value == '-' ||
           value == '.' || value == ':';
  });
  if (!valid) {
    throw std::invalid_argument("coordinate frame has unsupported characters");
  }
}

auto require_finite_vector(const std::array<double, 3>& values,
                           std::string_view field) -> void {
  for (const auto value : values) {
    require_finite(value, field);
  }
}

auto eigen_quaternion(const UnitQuaternion& value) -> Eigen::Quaterniond {
  return Eigen::Quaterniond(value.w, value.x, value.y, value.z);
}

auto sophus_transform(const RigidTransform& value) -> Sophus::SE3d {
  return Sophus::SE3d(
      Sophus::SO3d(eigen_quaternion(value.rotation)),
      Eigen::Vector3d(value.translation_m[0], value.translation_m[1],
                      value.translation_m[2]));
}

auto stored_quaternion(const Eigen::Quaterniond& value) -> UnitQuaternion {
  return make_unit_quaternion(value.w(), value.x(), value.y(), value.z());
}

auto require_global_altitude(const GlobalCoordinate& point,
                             std::string_view role) -> double {
  if (!point.altitude_m.has_value() ||
      point.vertical_datum != VerticalDatum::wgs84_ellipsoid) {
    throw std::invalid_argument(std::string(role) +
                                " requires WGS84 ellipsoidal altitude");
  }
  return *point.altitude_m;
}

}  // namespace

auto make_unit_quaternion(double w, double x, double y, double z)
    -> UnitQuaternion {
  require_finite(w, "quaternion w");
  require_finite(x, "quaternion x");
  require_finite(y, "quaternion y");
  require_finite(z, "quaternion z");
  Eigen::Quaterniond quaternion(w, x, y, z);
  const double norm = quaternion.norm();
  if (!std::isfinite(norm) || norm < 1e-12) {
    throw std::invalid_argument("quaternion norm is not recoverable");
  }
  const double deviation = std::abs(norm - 1.0);
  if (deviation > kQuaternionNormDeviationTolerance) {
    throw std::invalid_argument(
        "quaternion norm deviation exceeds the frozen tolerance");
  }
  quaternion.normalize();
  const bool negative_canonical_sign =
      quaternion.w() < 0.0 ||
      (quaternion.w() == 0.0 && quaternion.x() < 0.0) ||
      (quaternion.w() == 0.0 && quaternion.x() == 0.0 &&
       quaternion.y() < 0.0) ||
      (quaternion.w() == 0.0 && quaternion.x() == 0.0 &&
       quaternion.y() == 0.0 && quaternion.z() < 0.0);
  if (negative_canonical_sign) {
    quaternion.coeffs() *= -1.0;
  }
  return UnitQuaternion{quaternion.w(), quaternion.x(), quaternion.y(),
                        quaternion.z(), deviation};
}

auto quaternion_from_rotation_matrix(
    const std::array<double, 9>& row_major_values) -> UnitQuaternion {
  for (const auto value : row_major_values) {
    require_finite(value, "rotation matrix value");
  }
  Eigen::Matrix3d rotation;
  for (Eigen::Index row = 0; row < 3; ++row) {
    for (Eigen::Index column = 0; column < 3; ++column) {
      const auto index = static_cast<std::size_t>(row * 3 + column);
      rotation(row, column) = row_major_values[index];
    }
  }
  const double orthonormality_error =
      (rotation.transpose() * rotation - Eigen::Matrix3d::Identity()).norm();
  const double determinant = rotation.determinant();
  if (orthonormality_error > kRotationOrthonormalityTolerance ||
      std::abs(determinant - 1.0) > kRotationOrthonormalityTolerance ||
      determinant <= 0.0) {
    throw std::invalid_argument("matrix is not a proper rigid rotation");
  }
  return stored_quaternion(Eigen::Quaterniond(rotation));
}

auto make_rigid_transform(std::string target_frame, std::string source_frame,
                          std::array<double, 3> translation_m,
                          UnitQuaternion rotation) -> RigidTransform {
  require_frame(target_frame);
  require_frame(source_frame);
  require_finite_vector(translation_m, "translation coordinate");
  const auto normalized =
      make_unit_quaternion(rotation.w, rotation.x, rotation.y, rotation.z);
  return RigidTransform{std::move(target_frame), std::move(source_frame),
                        translation_m, normalized};
}

auto compose(const RigidTransform& outer, const RigidTransform& inner)
    -> RigidTransform {
  if (outer.source_frame != inner.target_frame) {
    throw std::invalid_argument(
        "transform chain requires outer source frame to equal inner target frame");
  }
  const Sophus::SE3d result =
      sophus_transform(outer) * sophus_transform(inner);
  const auto& translation = result.translation();
  return make_rigid_transform(
      outer.target_frame, inner.source_frame,
      {translation.x(), translation.y(), translation.z()},
      stored_quaternion(result.unit_quaternion()));
}

auto inverse(const RigidTransform& transform) -> RigidTransform {
  const Sophus::SE3d result = sophus_transform(transform).inverse();
  const auto& translation = result.translation();
  return make_rigid_transform(
      transform.source_frame, transform.target_frame,
      {translation.x(), translation.y(), translation.z()},
      stored_quaternion(result.unit_quaternion()));
}

auto interpolate(const RigidTransform& begin, const RigidTransform& end,
                 double fraction) -> RigidTransform {
  require_finite(fraction, "interpolation fraction");
  if (fraction < 0.0 || fraction > 1.0) {
    throw std::invalid_argument("transform extrapolation is unsupported");
  }
  if (begin.target_frame != end.target_frame ||
      begin.source_frame != end.source_frame) {
    throw std::invalid_argument(
        "interpolated transforms must have identical named frames");
  }
  std::array<double, 3> translation{};
  for (std::size_t index = 0; index < translation.size(); ++index) {
    translation[index] = begin.translation_m[index] +
                         fraction *
                             (end.translation_m[index] - begin.translation_m[index]);
  }
  const Eigen::Quaterniond rotation =
      eigen_quaternion(begin.rotation)
          .slerp(fraction, eigen_quaternion(end.rotation));
  return make_rigid_transform(begin.target_frame, begin.source_frame,
                              translation, stored_quaternion(rotation));
}

auto transform_point(const RigidTransform& transform,
                     const std::array<double, 3>& point_source)
    -> std::array<double, 3> {
  require_finite_vector(point_source, "source point coordinate");
  const Eigen::Vector3d point(point_source[0], point_source[1], point_source[2]);
  const Eigen::Vector3d transformed = sophus_transform(transform) * point;
  return {transformed.x(), transformed.y(), transformed.z()};
}

auto make_global_coordinate(double latitude_deg, double longitude_deg,
                            std::optional<double> altitude_m,
                            VerticalDatum vertical_datum) -> GlobalCoordinate {
  require_finite(latitude_deg, "latitude_deg");
  require_finite(longitude_deg, "longitude_deg");
  if (latitude_deg < -90.0 || latitude_deg > 90.0) {
    throw std::invalid_argument("latitude_deg must be within [-90, 90]");
  }
  if (longitude_deg < -180.0 || longitude_deg > 180.0) {
    throw std::invalid_argument("longitude_deg must be within [-180, 180]");
  }
  if (altitude_m.has_value()) {
    require_finite(*altitude_m, "altitude_m");
  }
  if (vertical_datum == VerticalDatum::wgs84_ellipsoid &&
      !altitude_m.has_value()) {
    throw std::invalid_argument(
        "WGS84 ellipsoidal vertical datum requires altitude_m");
  }
  return GlobalCoordinate{latitude_deg, longitude_deg, altitude_m,
                          vertical_datum};
}

auto global_to_local(const GlobalCoordinate& origin,
                     const GlobalCoordinate& point, std::string local_frame)
    -> LocalCoordinate {
  require_frame(local_frame);
  const auto origin_altitude = require_global_altitude(origin, "local origin");
  const auto point_altitude = require_global_altitude(point, "global point");
  GeographicLib::LocalCartesian local(origin.latitude_deg, origin.longitude_deg,
                                      origin_altitude);
  std::array<double, 3> position{};
  local.Forward(point.latitude_deg, point.longitude_deg, point_altitude,
                position[0], position[1], position[2]);
  require_finite_vector(position, "local coordinate");
  return LocalCoordinate{std::move(local_frame), position};
}

auto local_to_global(const GlobalCoordinate& origin,
                     const LocalCoordinate& point) -> GlobalCoordinate {
  require_frame(point.frame);
  require_finite_vector(point.position_m, "local coordinate");
  const auto origin_altitude = require_global_altitude(origin, "local origin");
  GeographicLib::LocalCartesian local(origin.latitude_deg, origin.longitude_deg,
                                      origin_altitude);
  double latitude = 0.0;
  double longitude = 0.0;
  double altitude = 0.0;
  local.Reverse(point.position_m[0], point.position_m[1], point.position_m[2],
                latitude, longitude, altitude);
  return make_global_coordinate(latitude, longitude, altitude,
                                VerticalDatum::wgs84_ellipsoid);
}

}  // namespace cartosentry::contracts
