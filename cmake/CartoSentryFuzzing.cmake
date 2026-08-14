function(cartosentry_enable_fuzzer_coverage target)
  if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    message(FATAL_ERROR "LibFuzzer coverage instrumentation requires Clang")
  endif()
  target_compile_options(
    "${target}"
    PRIVATE
      -fsanitize=fuzzer-no-link,address,undefined
      -fno-omit-frame-pointer
  )
endfunction()

function(cartosentry_enable_fuzzer target)
  if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    message(FATAL_ERROR "LibFuzzer targets require Clang")
  endif()
  target_compile_options(
    "${target}"
    PRIVATE
      -fsanitize=fuzzer,address,undefined
      -fno-omit-frame-pointer
  )
  target_link_options(
    "${target}"
    PRIVATE
      -fsanitize=fuzzer,address,undefined
  )
endfunction()
