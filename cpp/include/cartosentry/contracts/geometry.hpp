#pragma once

#include <array>
#include <optional>
#include <string>

namespace cartosentry::contracts {

inline constexpr double kQuaternionNormDeviationTolerance = 1e-6;
inline constexpr double kRotationOrthonormalityTolerance = 1e-9;

struct UnitQuaternion {
  double w{1.0};
  double x{};
  double y{};
  double z{};
  double pre_normalization_norm_deviation{};
};

struct RigidTransform {
  std::string target_frame;
  std::string source_frame;
  std::array<double, 3> translation_m{};
  UnitQuaternion rotation;
};

enum class VerticalDatum {
  wgs84_ellipsoid,
  unknown,
};

struct GlobalCoordinate {
  double latitude_deg{};
  double longitude_deg{};
  std::optional<double> altitude_m;
  VerticalDatum vertical_datum{VerticalDatum::unknown};
};

struct LocalCoordinate {
  std::string frame;
  std::array<double, 3> position_m{};
};

[[nodiscard]] auto make_unit_quaternion(double w, double x, double y, double z)
    -> UnitQuaternion;
[[nodiscard]] auto quaternion_from_rotation_matrix(
    const std::array<double, 9>& row_major_values) -> UnitQuaternion;
[[nodiscard]] auto make_rigid_transform(
    std::string target_frame, std::string source_frame,
    std::array<double, 3> translation_m, UnitQuaternion rotation)
    -> RigidTransform;
[[nodiscard]] auto compose(const RigidTransform& outer,
                           const RigidTransform& inner) -> RigidTransform;
[[nodiscard]] auto inverse(const RigidTransform& transform) -> RigidTransform;
[[nodiscard]] auto interpolate(const RigidTransform& begin,
                               const RigidTransform& end, double fraction)
    -> RigidTransform;
[[nodiscard]] auto transform_point(const RigidTransform& transform,
                                   const std::array<double, 3>& point_source)
    -> std::array<double, 3>;

[[nodiscard]] auto make_global_coordinate(
    double latitude_deg, double longitude_deg,
    std::optional<double> altitude_m, VerticalDatum vertical_datum)
    -> GlobalCoordinate;
[[nodiscard]] auto global_to_local(const GlobalCoordinate& origin,
                                   const GlobalCoordinate& point,
                                   std::string local_frame) -> LocalCoordinate;
[[nodiscard]] auto local_to_global(const GlobalCoordinate& origin,
                                   const LocalCoordinate& point)
    -> GlobalCoordinate;

}  // namespace cartosentry::contracts
