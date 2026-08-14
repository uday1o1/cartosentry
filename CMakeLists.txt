cmake_minimum_required(VERSION 3.28...4.4)

project(CartoSentry VERSION 0.1.0 LANGUAGES C CXX)

option(CARTOSENTRY_BUILD_PYTHON "Build the Python extension" ON)
option(
  CARTOSENTRY_BUILD_COMPATIBILITY_PROBE
  "Build the locked native dependency compatibility probe"
  OFF
)
option(CARTOSENTRY_ENABLE_SANITIZERS "Enable address and undefined sanitizers" OFF)
option(CARTOSENTRY_ENABLE_COVERAGE "Enable compiler coverage instrumentation" OFF)
option(CARTOSENTRY_ENABLE_CUDA "Enable the optional CUDA backend" OFF)

if(CARTOSENTRY_ENABLE_CUDA)
  message(FATAL_ERROR "CUDA is not selected until the M12 profiling gate")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

include(CTest)
include(cmake/CartoSentryWarnings.cmake)
include(cmake/CartoSentrySanitizers.cmake)

if(CARTOSENTRY_BUILD_PYTHON OR CARTOSENTRY_BUILD_COMPATIBILITY_PROBE)
  find_package(Python 3.12 EXACT COMPONENTS Interpreter Development.Module REQUIRED)
endif()

include(cmake/CartoSentryDependencies.cmake)

add_library(
  CartoSentryCore STATIC
  cpp/src/boreas_inspector.cpp
  cpp/src/geometry.cpp
  cpp/src/native_check.cpp
  cpp/src/observability.cpp
  cpp/src/time.cpp
)
add_library(CartoSentry::Core ALIAS CartoSentryCore)
target_include_directories(
  CartoSentryCore
  PUBLIC
    "$<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/cpp/include>"
    "$<INSTALL_INTERFACE:include>"
)
target_link_libraries(
  CartoSentryCore
  PUBLIC
    CartoSentry::Osmium
    CartoSentry::Sophus
    GeographicLib::GeographicLib
)
target_compile_features(CartoSentryCore PUBLIC cxx_std_20)
cartosentry_enable_warnings(CartoSentryCore)
cartosentry_enable_sanitizers(CartoSentryCore)
cartosentry_enable_coverage(CartoSentryCore)

if(CARTOSENTRY_BUILD_PYTHON)
  find_package(pybind11 3.1 CONFIG REQUIRED)
  pybind11_add_module(_core MODULE cpp/bindings/module.cpp)
  target_link_libraries(_core PRIVATE CartoSentry::Core)
  target_compile_features(_core PRIVATE cxx_std_20)
  cartosentry_enable_warnings(_core)
  cartosentry_enable_sanitizers(_core)
  cartosentry_enable_coverage(_core)
  install(TARGETS _core LIBRARY DESTINATION cartosentry)
endif()

if(CARTOSENTRY_BUILD_COMPATIBILITY_PROBE)
  add_executable(
    cartosentry_dependency_compatibility
    cpp/tests/dependency_compatibility.cpp
  )
  target_link_libraries(
    cartosentry_dependency_compatibility
    PRIVATE
      CartoSentry::Arrow
      CartoSentry::SQLite
      GeographicLib::GeographicLib
      fmt::fmt
      nlohmann_json::nlohmann_json
      opencv_core
      spdlog::spdlog
      yaml-cpp::yaml-cpp
  )
  target_include_directories(
    cartosentry_dependency_compatibility
    SYSTEM PRIVATE
      "${cartosentry_opencv_source_SOURCE_DIR}/modules/core/include"
      "${PROJECT_BINARY_DIR}"
  )
  set_property(
    TARGET cartosentry_dependency_compatibility
    PROPERTY BUILD_RPATH "${CARTOSENTRY_PYARROW_LIBRARY_DIR}"
  )
  target_compile_features(cartosentry_dependency_compatibility PRIVATE cxx_std_20)
  cartosentry_enable_warnings(cartosentry_dependency_compatibility)
  cartosentry_enable_sanitizers(cartosentry_dependency_compatibility)
  cartosentry_enable_coverage(cartosentry_dependency_compatibility)
  add_test(
    NAME dependency-compatibility
    COMMAND cartosentry_dependency_compatibility
  )
endif()

if(BUILD_TESTING)
  add_executable(
    cartosentry_core_tests
    cpp/tests/boreas_inspector_test.cpp
    cpp/tests/contracts_test.cpp
    cpp/tests/native_check_test.cpp
    cpp/tests/observability_test.cpp
  )
  target_link_libraries(cartosentry_core_tests PRIVATE CartoSentry::Core Catch2::Catch2WithMain)
  target_compile_features(cartosentry_core_tests PRIVATE cxx_std_20)
  cartosentry_enable_warnings(cartosentry_core_tests)
  cartosentry_enable_sanitizers(cartosentry_core_tests)
  cartosentry_enable_coverage(cartosentry_core_tests)
  include(Catch)
  catch_discover_tests(cartosentry_core_tests)
endif()
