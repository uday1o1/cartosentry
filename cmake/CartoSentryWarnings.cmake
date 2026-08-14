function(cartosentry_enable_warnings target)
  if(MSVC)
    target_compile_options("${target}" PRIVATE /W4 /WX /permissive-)
  else()
    target_compile_options(
      "${target}" PRIVATE
      -Wall
      -Wextra
      -Wpedantic
      -Wconversion
      -Wsign-conversion
      -Wshadow
      -Werror
      "-ffile-prefix-map=${PROJECT_SOURCE_DIR}=."
      "-ffile-prefix-map=${PROJECT_BINARY_DIR}=."
      "-fmacro-prefix-map=${PROJECT_SOURCE_DIR}=."
      "-fmacro-prefix-map=${PROJECT_BINARY_DIR}=."
    )
  endif()
endfunction()
