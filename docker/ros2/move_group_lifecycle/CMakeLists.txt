cmake_minimum_required(VERSION 3.16)
project(relay_moveit_runtime)
find_package(ament_cmake REQUIRED)
find_package(moveit_ros_move_group 2.5.10 EXACT REQUIRED)
find_package(moveit_ros_planning 2.5.10 EXACT REQUIRED)
find_package(rclcpp REQUIRED)
find_package(tf2_ros REQUIRED)
add_executable(relay_move_group move_group.cpp)
set_target_properties(relay_move_group PROPERTIES OUTPUT_NAME move_group)
target_compile_features(relay_move_group PRIVATE cxx_std_17)
ament_target_dependencies(relay_move_group moveit_ros_move_group moveit_ros_planning rclcpp tf2_ros)
# ROS callback objects can outlive the capability loader. Keep their compiled
# callback/destructor code mapped until process teardown, like any normal shared
# dependency, rather than allowing pluginlib to dlclose it before Node teardown.
target_link_libraries(relay_move_group
  "-Wl,--no-as-needed" moveit_ros_move_group::moveit_move_group_default_capabilities
  "-Wl,--as-needed")
install(TARGETS relay_move_group DESTINATION lib/${PROJECT_NAME})
install(FILES UPSTREAM-LICENSE.txt DESTINATION share/${PROJECT_NAME})
ament_package()
