include(FetchContent)

set(_cartosentry_lock_file "${PROJECT_SOURCE_DIR}/cmake/dependencies.lock.json")
file(READ "${_cartosentry_lock_file}" _cartosentry_dependency_lock)

function(cartosentry_locked_dependency_field output dependency field)
  string(
    JSON value ERROR_VARIABLE error
    GET "${_cartosentry_dependency_lock}" dependencies "${dependency}" "${field}"
  )
  if(error)
    message(FATAL_ERROR "Invalid dependency lock entry ${dependency}.${field}: ${error}")
  endif()
  set("${output}" "${value}" PARENT_SCOPE)
endfunction()

function(cartosentry_declare_locked_archive content_name dependency)
  cartosentry_locked_dependency_field(archive_url "${dependency}" archive_url)
  cartosentry_locked_dependency_field(archive_sha256 "${dependency}" archive_sha256)
  FetchContent_Declare(
    "${content_name}"
    URL "${archive_url}"
    URL_HASH "SHA256=${archive_sha256}"
    DOWNLOAD_EXTRACT_TIMESTAMP FALSE
  )
endfunction()

cartosentry_locked_dependency_field(eigen_archive eigen archive_url)
cartosentry_locked_dependency_field(eigen_archive_sha256 eigen archive_sha256)
FetchContent_Declare(
  cartosentry_eigen_source
  URL "${eigen_archive}"
  URL_HASH "SHA256=${eigen_archive_sha256}"
  DOWNLOAD_EXTRACT_TIMESTAMP FALSE
  SOURCE_SUBDIR cartosentry-no-build
)

cartosentry_locked_dependency_field(sophus_archive sophus archive_url)
cartosentry_locked_dependency_field(sophus_archive_sha256 sophus archive_sha256)
FetchContent_Declare(
  cartosentry_sophus_source
  URL "${sophus_archive}"
  URL_HASH "SHA256=${sophus_archive_sha256}"
  DOWNLOAD_EXTRACT_TIMESTAMP FALSE
  SOURCE_SUBDIR cartosentry-no-build
)

FetchContent_MakeAvailable(cartosentry_eigen_source cartosentry_sophus_source)

cartosentry_locked_dependency_field(geographiclib_archive geographiclib archive_url)
cartosentry_locked_dependency_field(
  geographiclib_archive_sha256 geographiclib archive_sha256
)
FetchContent_Declare(
  cartosentry_geographiclib_source
  URL "${geographiclib_archive}"
  URL_HASH "SHA256=${geographiclib_archive_sha256}"
  DOWNLOAD_EXTRACT_TIMESTAMP FALSE
  SOURCE_SUBDIR cartosentry-no-build
)
FetchContent_MakeAvailable(cartosentry_geographiclib_source)

file(GLOB geographiclib_sources CONFIGURE_DEPENDS
  "${cartosentry_geographiclib_source_SOURCE_DIR}/src/*.cpp"
)
set(geographiclib_generated_include
  "${PROJECT_BINARY_DIR}/cartosentry-generated/geographiclib"
)
file(MAKE_DIRECTORY "${geographiclib_generated_include}/GeographicLib")
configure_file(
  "${PROJECT_SOURCE_DIR}/cmake/GeographicLibConfig.h.in"
  "${geographiclib_generated_include}/GeographicLib/Config.h"
  @ONLY
)
add_library(CartoSentryGeographicLib STATIC ${geographiclib_sources})
add_library(GeographicLib::GeographicLib ALIAS CartoSentryGeographicLib)
target_include_directories(
  CartoSentryGeographicLib SYSTEM PUBLIC
    "${cartosentry_geographiclib_source_SOURCE_DIR}/include"
    "${geographiclib_generated_include}"
)
target_compile_definitions(CartoSentryGeographicLib PUBLIC GEOGRAPHICLIB_SHARED_LIB=0)

add_library(CartoSentryEigen INTERFACE)
add_library(CartoSentry::Eigen ALIAS CartoSentryEigen)
target_include_directories(
  CartoSentryEigen SYSTEM INTERFACE
  "${cartosentry_eigen_source_SOURCE_DIR}"
)

add_library(CartoSentrySophus INTERFACE)
add_library(CartoSentry::Sophus ALIAS CartoSentrySophus)
target_include_directories(
  CartoSentrySophus SYSTEM INTERFACE
  "${cartosentry_sophus_source_SOURCE_DIR}"
)
target_link_libraries(CartoSentrySophus INTERFACE CartoSentry::Eigen)

cartosentry_locked_dependency_field(libosmium_archive libosmium archive_url)
cartosentry_locked_dependency_field(
  libosmium_archive_sha256 libosmium archive_sha256
)
FetchContent_Declare(
  cartosentry_libosmium_source
  URL "${libosmium_archive}"
  URL_HASH "SHA256=${libosmium_archive_sha256}"
  DOWNLOAD_EXTRACT_TIMESTAMP FALSE
  SOURCE_SUBDIR cartosentry-no-build
)
FetchContent_MakeAvailable(cartosentry_libosmium_source)
find_package(BZip2 REQUIRED)
find_package(EXPAT REQUIRED)
find_package(Threads REQUIRED)
find_package(ZLIB REQUIRED)
add_library(CartoSentryOsmium INTERFACE)
add_library(CartoSentry::Osmium ALIAS CartoSentryOsmium)
target_include_directories(
  CartoSentryOsmium SYSTEM INTERFACE
  "${cartosentry_libosmium_source_SOURCE_DIR}/include"
)
target_link_libraries(
  CartoSentryOsmium INTERFACE
  BZip2::BZip2 EXPAT::EXPAT Threads::Threads ZLIB::ZLIB
)

if(BUILD_TESTING)
  cartosentry_locked_dependency_field(catch_archive catch2 archive_url)
  cartosentry_locked_dependency_field(catch_archive_sha256 catch2 archive_sha256)
  set(CATCH_BUILD_TESTING OFF CACHE BOOL "" FORCE)
  set(CATCH_INSTALL_DOCS OFF CACHE BOOL "" FORCE)
  set(CATCH_INSTALL_EXTRAS OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(
    Catch2
    URL "${catch_archive}"
    URL_HASH "SHA256=${catch_archive_sha256}"
    DOWNLOAD_EXTRACT_TIMESTAMP FALSE
  )
  FetchContent_MakeAvailable(Catch2)
endif()

if(CARTOSENTRY_BUILD_COMPATIBILITY_PROBE)
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
  set(BUILD_DOCUMENTATION OFF CACHE BOOL "" FORCE)
  set(BUILD_MANPAGES OFF CACHE BOOL "" FORCE)
  set(CONVERT_WARNINGS_TO_ERRORS OFF CACHE BOOL "" FORCE)
  set(FMT_DOC OFF CACHE BOOL "" FORCE)
  set(FMT_TEST OFF CACHE BOOL "" FORCE)
  set(GEOGRAPHICLIB_DATA "" CACHE PATH "" FORCE)
  set(OPENCV_ENABLE_ALLOCATOR_STATS OFF CACHE BOOL "" FORCE)
  set(OPENCV_GENERATE_PKGCONFIG OFF CACHE BOOL "" FORCE)
  set(BUILD_LIST "core" CACHE STRING "" FORCE)
  set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(BUILD_OPENJPEG OFF CACHE BOOL "" FORCE)
  set(BUILD_opencv_apps OFF CACHE BOOL "" FORCE)
  set(BUILD_opencv_python_bindings_generator OFF CACHE BOOL "" FORCE)
  set(BUILD_PERF_TESTS OFF CACHE BOOL "" FORCE)
  set(BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(BUILD_WITH_DEBUG_INFO OFF CACHE BOOL "" FORCE)
  set(BUILD_ZLIB OFF CACHE BOOL "" FORCE)
  set(WITH_ADE OFF CACHE BOOL "" FORCE)
  set(WITH_EIGEN OFF CACHE BOOL "" FORCE)
  set(WITH_FFMPEG OFF CACHE BOOL "" FORCE)
  set(WITH_GSTREAMER OFF CACHE BOOL "" FORCE)
  set(WITH_IPP OFF CACHE BOOL "" FORCE)
  set(WITH_ITT OFF CACHE BOOL "" FORCE)
  set(WITH_JASPER OFF CACHE BOOL "" FORCE)
  set(WITH_JPEG OFF CACHE BOOL "" FORCE)
  set(WITH_KLEIDICV OFF CACHE BOOL "" FORCE)
  set(WITH_OPENCL OFF CACHE BOOL "" FORCE)
  set(WITH_OPENEXR OFF CACHE BOOL "" FORCE)
  set(WITH_OPENJPEG OFF CACHE BOOL "" FORCE)
  set(WITH_PNG OFF CACHE BOOL "" FORCE)
  set(WITH_PROTOBUF OFF CACHE BOOL "" FORCE)
  set(WITH_TBB OFF CACHE BOOL "" FORCE)
  set(WITH_TIFF OFF CACHE BOOL "" FORCE)
  set(WITH_WEBP OFF CACHE BOOL "" FORCE)
  set(SPDLOG_BUILD_EXAMPLE OFF CACHE BOOL "" FORCE)
  set(SPDLOG_BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(SPDLOG_FMT_EXTERNAL ON CACHE BOOL "" FORCE)
  set(YAML_CPP_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
  set(YAML_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(YAML_CPP_BUILD_TOOLS OFF CACHE BOOL "" FORCE)

  cartosentry_declare_locked_archive(cartosentry_fmt_source fmt)
  cartosentry_declare_locked_archive(cartosentry_json_source nlohmann-json)
  cartosentry_declare_locked_archive(cartosentry_opencv_source opencv)
  cartosentry_declare_locked_archive(cartosentry_spdlog_source spdlog)
  cartosentry_declare_locked_archive(cartosentry_sqlite_source sqlite)
  cartosentry_declare_locked_archive(cartosentry_yaml_source yaml-cpp)

  FetchContent_MakeAvailable(
    cartosentry_fmt_source
    cartosentry_json_source
    cartosentry_opencv_source
    cartosentry_spdlog_source
    cartosentry_sqlite_source
    cartosentry_yaml_source
  )
  foreach(external_target IN ITEMS fmt nlohmann_json opencv_core spdlog yaml-cpp)
    set_property(TARGET "${external_target}" PROPERTY SYSTEM TRUE)
  endforeach()

  add_library(CartoSentrySQLite STATIC "${cartosentry_sqlite_source_SOURCE_DIR}/sqlite3.c")
  add_library(CartoSentry::SQLite ALIAS CartoSentrySQLite)
  target_include_directories(
    CartoSentrySQLite SYSTEM PUBLIC "${cartosentry_sqlite_source_SOURCE_DIR}"
  )
  target_compile_definitions(
    CartoSentrySQLite
    PRIVATE SQLITE_DQS=0 SQLITE_OMIT_DEPRECATED SQLITE_THREADSAFE=1
  )

  execute_process(
    COMMAND
      "${Python_EXECUTABLE}" -c
      "import pathlib, pyarrow; print(pathlib.Path(pyarrow.__file__).parent); print(pyarrow.__version__)"
    RESULT_VARIABLE pyarrow_result
    OUTPUT_VARIABLE pyarrow_output
    ERROR_VARIABLE pyarrow_error
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
  if(NOT pyarrow_result EQUAL 0)
    message(FATAL_ERROR "Unable to inspect locked PyArrow: ${pyarrow_error}")
  endif()
  string(REPLACE "\n" ";" pyarrow_fields "${pyarrow_output}")
  list(GET pyarrow_fields 0 CARTOSENTRY_PYARROW_LIBRARY_DIR)
  list(GET pyarrow_fields 1 pyarrow_version)
  cartosentry_locked_dependency_field(locked_arrow_version apache-arrow version)
  if(NOT pyarrow_version STREQUAL locked_arrow_version)
    message(
      FATAL_ERROR
      "PyArrow ${pyarrow_version} does not match locked Arrow ${locked_arrow_version}"
    )
  endif()

  file(GLOB pyarrow_library_candidates
    "${CARTOSENTRY_PYARROW_LIBRARY_DIR}/libarrow.*"
  )
  foreach(candidate IN LISTS pyarrow_library_candidates)
    get_filename_component(candidate_name "${candidate}" NAME)
    if(candidate_name MATCHES "^libarrow(\\.[0-9]+)?\\.dylib$|^libarrow\\.so(\\.[0-9]+)?$")
      set(pyarrow_library "${candidate}")
      break()
    endif()
  endforeach()
  if(NOT pyarrow_library)
    message(FATAL_ERROR "The locked PyArrow wheel has no native Arrow library")
  endif()

  add_library(CartoSentryArrow SHARED IMPORTED GLOBAL)
  add_library(CartoSentry::Arrow ALIAS CartoSentryArrow)
  set_target_properties(
    CartoSentryArrow
    PROPERTIES
      IMPORTED_LOCATION "${pyarrow_library}"
      INTERFACE_INCLUDE_DIRECTORIES "${CARTOSENTRY_PYARROW_LIBRARY_DIR}/include"
  )
endif()
