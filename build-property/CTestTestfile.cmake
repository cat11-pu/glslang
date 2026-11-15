# CMake generated Testfile for 
# Source directory: /home/cat/gsb/uid3857
# Build directory: /home/cat/gsb/uid3857/build-property
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test([=[glslang-testsuite]=] "bash" "runtests" "/home/cat/gsb/uid3857/build-property/localResults" "/home/cat/gsb/uid3857/build-property/StandAlone/glslang" "1")
set_tests_properties([=[glslang-testsuite]=] PROPERTIES  WORKING_DIRECTORY "/home/cat/gsb/uid3857/Test/" _BACKTRACE_TRIPLES "/home/cat/gsb/uid3857/CMakeLists.txt;380;add_test;/home/cat/gsb/uid3857/CMakeLists.txt;0;")
subdirs("External")
subdirs("SPIRV")
subdirs("glslang")
subdirs("StandAlone")
subdirs("gtests")
