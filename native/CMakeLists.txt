cmake_minimum_required(VERSION 3.16)
project(jeeves_layershell LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC OFF)

find_package(Qt6 REQUIRED COMPONENTS Core Gui)
find_package(LayerShellQt REQUIRED)

add_library(jeeves_layershell SHARED jeeves_layershell.cpp)
target_link_libraries(jeeves_layershell PRIVATE Qt6::Core Qt6::Gui LayerShellQt::Interface)

install(TARGETS jeeves_layershell LIBRARY DESTINATION lib)
