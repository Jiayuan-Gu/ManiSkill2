from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Union

import gym
import numpy as np
import sapien.core as sapien
from sapien.utils import Viewer

from mani_skill2 import logger
from mani_skill2.agents.base_agent import BaseAgent, AgentConfig
from mani_skill2.agents.camera import get_camera_images, get_camera_pcd, get_camera_rgb
from mani_skill2.utils.common import (
    convert_observation_to_space,
    flatten_state_dict,
    merge_dicts,
)
from mani_skill2.utils.sapien_utils import (
    get_actor_state,
    get_articulation_state,
    set_actor_state,
    set_articulation_state,
)
from mani_skill2.utils.trimesh_utils import (
    get_actor_meshes,
    get_articulation_meshes,
    merge_meshes,
)
from mani_skill2.utils.visualization.misc import (
    observations_to_images,
    tile_images,
)
from mani_skill2.sensors.camera import Camera, CameraConfig


class BaseEnv(gym.Env):
    """Superclass for ManiSkill environments.

    Args:
        obs_mode: observation mode registered in @SUPPORTED_OBS_MODES.
        reward_mode: reward mode registered in @SUPPORTED_REWARD_MODES.
        control_mode: controll mode of the agent.
            "*" represents all registered controllers and action space is a dict.
        sim_freq (int): simulation frequency (Hz)
        control_freq (int): control frequency (Hz)
        renderer_type (str): type of renderer. Support "vulkan" or "kuafu".
            `KuafuRenderer` (ray-tracing) is only experimentally and partially supported now.
        device (str): GPU device for renderer, e.g., 'cuda:x'.
        camera_cfgs (Dict[str, Dict]): configurations of cameras. See notes for more details.
        enable_shadow (bool): whether to enable shadow for lights. Defaults to False.
        enable_gt_seg (bool): whether to include GT segmentaiton masks in observations. Defaults to False.

    Keyword Args:
        vulkan_kwargs (dict): kwargs to initialize `VulkanRenderer`.
        kuafu_kwargs (dict): kwargs to initialize `KuafuRenderer`.

    Note:
        `camera_cfgs` is used to update environement-specific camera configurations.
        If the key is one of reserved keywords ([]), the value will be applied to all cameras,
        unless that key is specified at a camera-level
    """

    # fmt: off
    SUPPORTED_OBS_MODES = (
        "state", "state_dict", "none", "rgbd", "pointcloud", 
        "rgbd_robot_seg", "pointcloud_robot_seg",
    )
    SUPPORTED_REWARD_MODES = ("dense", "sparse")
    # fmt: on

    agent: BaseAgent
    _agent_cfg: AgentConfig
    # _cameras: Dict[str, sapien.CameraEntity]
    _cameras: Dict[str, Camera]
    _camera_cfgs: Dict[str, CameraConfig]

    def __init__(
        self,
        obs_mode=None,
        reward_mode=None,
        control_mode=None,
        sim_freq: int = 500,
        control_freq: int = 20,
        renderer_type="vulkan",
        device: str = "",
        camera_cfgs: List[dict] = None,
        enable_shadow=False,
        **kwargs,
    ):
        # Create SAPIEN engine
        self._engine = sapien.Engine()

        # Create SAPIEN renderer
        self._renderer_type = renderer_type
        if self._renderer_type == "vulkan":
            vulkan_kwargs = kwargs.get("vulkan_kwargs", {})
            self._renderer = sapien.VulkanRenderer(device=device, **vulkan_kwargs)
        elif self._renderer_type == "kuafu":
            kuafu_config = sapien.KuafuConfig()
            kuafu_kwargs = kwargs.get("kuafu_kwargs", {})
            for k, v in kuafu_kwargs.items():
                setattr(kuafu_config, k, v)
            self._renderer = sapien.KuafuRenderer(kuafu_config)
            logger.warning("Only rgb is supported by KuafuRenderer.")
        else:
            raise NotImplementedError(self._renderer_type)
        self._renderer.set_log_level("warn")

        self._engine.set_renderer(self._renderer)
        self._viewer = None

        # Set simulation and control frequency
        self._sim_freq = sim_freq
        self._control_freq = control_freq
        if sim_freq % control_freq != 0:
            logger.warning(
                f"sim_freq({sim_freq}) is not divisible by control_freq({control_freq}).",
            )
        self._sim_steps_per_control = sim_freq // control_freq

        # Observation mode
        if obs_mode is None:
            obs_mode = self.SUPPORTED_OBS_MODES[0]
        if obs_mode not in self.SUPPORTED_OBS_MODES:
            raise NotImplementedError("Unsupported obs mode: {}".format(obs_mode))
        self._obs_mode = obs_mode

        # Reward mode
        if reward_mode is None:
            reward_mode = self.SUPPORTED_REWARD_MODES[0]
        if reward_mode not in self.SUPPORTED_REWARD_MODES:
            raise NotImplementedError("Unsupported reward mode: {}".format(reward_mode))
        self._reward_mode = reward_mode

        # TODO(jigu): support dict action space and check whether the name is good
        # Control mode
        self._control_mode = control_mode

        # NOTE(jigu): Agent and camera configurations should not change after initialization.
        self._set_agent_cfg()
        self._set_camera_cfgs(camera_cfgs)

        # Lighting
        self.enable_shadow = enable_shadow

        # NOTE(jigu): `seed` is deprecated in the latest gym.
        # Use a fixed seed to initialize to enhance determinism
        self.seed(2022)
        obs = self.reset(reconfigure=True)
        # TODO(jigu): Does it still work for VulkanRPCRenderer?
        self.observation_space = convert_observation_to_space(obs)
        self.action_space = self.agent.action_space

    def seed(self, seed=None):
        # For each episode, seed can be passed through `reset(seed=...)`,
        # or generated by `_main_rng`
        if seed is None:
            # Explicitly generate a seed for reproducibility
            seed = np.random.RandomState().randint(2**32)
        self._main_seed = seed
        self._main_rng = np.random.RandomState(self._main_seed)
        return [self._main_seed]

    def _set_agent_cfg(self):
        # TODO(jigu): Support a dummy agent for simulation only
        raise NotImplementedError

    def _get_camera_cfgs(self) -> Dict[str, CameraConfig]:
        return {}

    def _set_camera_cfgs(self, cfg_dict: dict = None):
        self._camera_cfgs = self._get_camera_cfgs()
        if self._agent_cfg is not None:
            self._camera_cfgs.update(self._agent_cfg.cameras)
        if cfg_dict is not None:
            pass  # TODO(jigu): update as urdf parser

    @property
    def sim_freq(self):
        return self._sim_freq

    @property
    def control_freq(self):
        return self._control_freq

    @property
    def sim_timestep(self):
        return 1.0 / self._sim_freq

    @property
    def control_timestep(self):
        return 1.0 / self._control_freq

    @property
    def control_mode(self):
        return self.agent.control_mode

    @property
    def elapsed_steps(self):
        return self._elapsed_steps

    # ---------------------------------------------------------------------------- #
    # Observation
    # ---------------------------------------------------------------------------- #
    @property
    def obs_mode(self):
        return self._obs_mode

    def get_obs(self):
        if self._obs_mode == "none":  # for cases do not need obs, like MPC
            return OrderedDict()
        elif self._obs_mode == "state":
            state_dict = self._get_obs_state_dict()
            return flatten_state_dict(state_dict)
        elif self._obs_mode == "state_dict":
            return self._get_obs_state_dict()
        elif self._obs_mode == "rgbd":
            return self._get_obs_rgbd()
        elif self._obs_mode == "pointcloud":
            return self._get_obs_pointcloud()
        elif self._obs_mode == "rgbd_robot_seg":
            return self._get_obs_rgbd_robot_seg()
        elif self._obs_mode == "pointcloud_robot_seg":
            return self._get_obs_pointcloud_robot_seg()
        else:
            raise NotImplementedError(self._obs_mode)

    def _get_obs_state_dict(self) -> OrderedDict:
        """Get (GT) state-based observations."""
        return OrderedDict(
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(),
        )

    def _get_obs_agent(self) -> OrderedDict:
        """Get observations from the agent's sensors, e.g., proprioceptive sensors."""
        return self.agent.get_proprioception()

    def _get_obs_extra(self) -> OrderedDict:
        """Get task-relevant extra observations."""
        return OrderedDict()

    def _get_obs_rgbd(self, **kwargs) -> OrderedDict:
        # Overwrite options if using GT segmentation
        if self._enable_gt_seg:
            kwargs.update(visual_seg=True, actor_seg=True)

        # Overwrite options if using KuaFu renderer
        if self._enable_kuafu:
            kwargs.update(depth=False, visual_seg=False, actor_seg=False)

        return OrderedDict(
            image=self._get_obs_images(**kwargs),
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(),
        )

    def _get_obs_images(
        self, rgb=True, depth=True, visual_seg=False, actor_seg=False
    ) -> OrderedDict:
        """Get observations from cameras.

        Args:
            rgb: whether to include RGB
            depth: whether to include depth
            visual_seg: whether to include visual-level (most fine-grained) segmentation
            actor_seg: whether to include actor-level segmentation (indexed by actor id)

        Returns:
            OrderedDict: The key is the camera name, and the value is an OrderedDict
                containing camera observations (rgb, depth, ...).
                For example, {"hand_camera": {"rgb": array([H, W, 3], np.uint8)}}
        """
        self.update_render()

        # Take pictures first, which is non-blocking
        self.agent.take_picture()
        for camera in self._cameras.values():
            camera.take_picture()

        obs_dict = self.agent.get_camera_images(
            rgb=rgb, depth=depth, visual_seg=visual_seg, actor_seg=actor_seg
        )
        for name, camera in self._cameras.items():
            obs_dict[name] = self._get_camera_images(
                camera, rgb=rgb, depth=depth, visual_seg=visual_seg, actor_seg=actor_seg
            )
        return obs_dict

    def _get_camera_images(self, camera: sapien.CameraEntity, **kwargs) -> OrderedDict:
        obs_dict = get_camera_images(camera, **kwargs)
        obs_dict.update(
            camera_intrinsic=camera.get_intrinsic_matrix(),
            camera_extrinsic=camera.get_extrinsic_matrix(),
        )
        return obs_dict

    def _get_obs_pointcloud(self, **kwargs):
        """Fuse pointclouds from all cameras in the world frame."""
        # Overwrite options if using GT segmentation
        if self._enable_gt_seg:
            kwargs.update(visual_seg=True, actor_seg=True)

        # Overwrite options if using KuaFu renderer
        if self._enable_kuafu:
            raise NotImplementedError(
                "Do not support pointcloud mode for KuafuRenderer yet."
            )

        self.update_render()

        # Take pictures first, which is non-blocking
        self.agent.take_picture()
        for camera in self._cameras.values():
            camera.take_picture()

        pcds = self.agent.get_camera_pcd(fuse=False, **kwargs)
        for name, camera in self._cameras.items():
            pcd = get_camera_pcd(camera, **kwargs)
            T = camera.get_model_matrix()
            pcd["xyzw"] = pcd["xyzw"] @ T.T
            pcds[name] = pcd

        fused_pcd = merge_dicts(pcds.values(), True)

        return OrderedDict(
            pointcloud=fused_pcd,
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(),
        )

    def _get_robot_seg(self, actor_seg):
        """Get the segmentation mask of robot links."""
        mask = np.isin(actor_seg, self.agent.robot_link_ids)
        return actor_seg * mask

    def _get_obs_rgbd_robot_seg(self):
        obs = self._get_obs_rgbd(actor_seg=True)
        for image in obs["image"].values():
            image["robot_seg"] = self._get_robot_seg(image.pop("actor_seg"))
        return obs

    def _get_obs_pointcloud_robot_seg(self):
        obs = self._get_obs_pointcloud(actor_seg=True)
        pointcloud = obs["pointcloud"]
        pointcloud["robot_seg"] = self._get_robot_seg(pointcloud.pop("actor_seg"))
        return obs

    # -------------------------------------------------------------------------- #
    # Reward mode
    # -------------------------------------------------------------------------- #
    @property
    def reward_mode(self):
        return self._reward_mode

    def get_reward(self, **kwargs):
        if self._reward_mode == "sparse":
            eval_info = self.evaluate(**kwargs)
            return float(eval_info["success"])
        elif self._reward_mode == "dense":
            return self.compute_dense_reward(**kwargs)
        else:
            raise NotImplementedError(self._reward_mode)

    def compute_dense_reward(self, **kwargs):
        raise NotImplementedError

    # -------------------------------------------------------------------------- #
    # Reconfigure
    # -------------------------------------------------------------------------- #
    def reconfigure(self):
        """Reconfigure the simulation scene instance.
        This function should clear the previous scene, and create a new one.
        """
        self._clear()

        self._setup_scene()
        self._load_agent()
        self._load_actors()
        self._load_articulations()
        self._setup_cameras()
        self._setup_lighting()

        if self._viewer is not None:
            self._setup_viewer()

        # Cache actors and articulations
        self._actors = self.get_actors()
        self._articulations = self.get_articulations()

    def _add_ground(self, altitude=0.0, render=True):
        if render:
            rend_mtl = self._renderer.create_material()
            rend_mtl.base_color = [0.06, 0.08, 0.12, 1]
            rend_mtl.metallic = 0.0
            rend_mtl.roughness = 0.9
            rend_mtl.specular = 0.8
        else:
            rend_mtl = None
        return self._scene.add_ground(
            altitude=altitude,
            render=render,
            render_material=rend_mtl,
        )

    def _load_actors(self):
        pass

    def _load_articulations(self):
        pass

    def _load_agent(self):
        pass

    def _setup_cameras(self):
        self._cameras = OrderedDict()
        for uuid, camera_cfg in self._camera_cfgs.items():
            self._cameras[uuid] = Camera(camera_cfg, self._scene, self._renderer_type)

    def _setup_lighting(self):
        shadow = self.enable_shadow
        self._scene.set_ambient_light([0.3, 0.3, 0.3])
        # Only the first of directional lights can have shadow
        self._scene.add_directional_light(
            [1, 1, -1], [1, 1, 1], shadow=shadow, scale=5, shadow_map_size=2048
        )
        self._scene.add_directional_light([0, 0, -1], [1, 1, 1])

    # -------------------------------------------------------------------------- #
    # Reset
    # -------------------------------------------------------------------------- #
    def reset(self, seed=None, reconfigure=False):
        self.set_episode_rng(seed)
        self._elapsed_steps = 0

        if reconfigure:
            # Reconfigure the scene if assets change
            self.reconfigure()
        else:
            self._clear_sim_state()

        # To guarantee seed reproducibility
        self.set_episode_rng(self._episode_seed)
        self.initialize_episode()

        return self.get_obs()

    def set_episode_rng(self, seed):
        """Set the random generator for current episode."""
        if seed is None:
            self._episode_seed = self._main_rng.randint(2**32)
        else:
            self._episode_seed = seed
        self._episode_rng = np.random.RandomState(self._episode_seed)

    def initialize_episode(self):
        """Initialize the episode, e.g., poses of actors and articulations, and robot configuration.
        No new assets are created. Task-relevant information can be initialized here, like goals.
        """
        self._initialize_actors()
        self._initialize_articulations()
        self._initialize_agent()
        self._initialize_task()

    def _initialize_actors(self):
        """Initialize the poses of actors."""
        pass

    def _initialize_articulations(self):
        """Initialize the (joint) poses of articulations."""
        pass

    def _initialize_agent(self):
        """Initialize the (joint) poses of agent(robot)."""
        pass

    def _initialize_task(self):
        """Initialize task-relevant information, like goals."""
        pass

    def _clear_sim_state(self):
        """Clear simulation state (velocities)"""
        for actor in self._scene.get_all_actors():
            if actor.type != "static":
                # TODO(fxiang): kinematic actor may need another way.
                actor.set_velocity([0, 0, 0])
                actor.set_angular_velocity([0, 0, 0])
        for articulation in self._scene.get_all_articulations():
            articulation.set_qvel(np.zeros(articulation.dof))
            articulation.set_root_velocity([0, 0, 0])
            articulation.set_root_angular_velocity([0, 0, 0])

    # -------------------------------------------------------------------------- #
    # Step
    # -------------------------------------------------------------------------- #
    def step(self, action: Union[None, np.ndarray, Dict]):
        self.step_action(action)
        self._elapsed_steps += 1

        obs = self.get_obs()
        info = self.get_info(obs=obs)
        reward = self.get_reward(obs=obs, action=action, info=info)
        done = self.get_done(obs=obs, info=info)

        return obs, reward, done, info

    def step_action(self, action):
        if action is None:  # simulation without action
            pass
        elif isinstance(action, np.ndarray):
            self.agent.set_action(action)
        elif isinstance(action, dict):
            if action["control_mode"] != self.agent.control_mode:
                self.agent.set_control_mode(action["control_mode"])
            self.agent.set_action(action["action"])
        else:
            raise TypeError(type(action))

        self._before_control_step()
        for _ in range(self._sim_steps_per_control):
            self.agent.before_simulation_step()
            self._scene.step()
            self._after_simulation_step()

    def evaluate(self, **kwargs) -> dict:
        """Evaluate whether the task succeeds."""
        raise NotImplementedError

    def get_done(self, info: dict, **kwargs):
        # NOTE(jigu): cast to bool explicitly for gym >=0.24
        return bool(info["success"])

    def get_info(self, **kwargs):
        info = dict(elapsed_steps=self._elapsed_steps)
        info.update(self.evaluate(**kwargs))
        return info

    def _before_control_step(self):
        pass

    def _after_simulation_step(self):
        pass

    # -------------------------------------------------------------------------- #
    # Simulation and other gym interfaces
    # -------------------------------------------------------------------------- #
    def _get_default_scene_config(self):
        scene_config = sapien.SceneConfig()
        scene_config.default_dynamic_friction = 1.0
        scene_config.default_static_friction = 1.0
        scene_config.default_restitution = 0.0
        scene_config.contact_offset = 0.02
        scene_config.enable_pcm = False
        scene_config.solver_iterations = 25
        scene_config.solver_velocity_iterations = 0
        return scene_config

    def _setup_scene(self, scene_config: Optional[sapien.SceneConfig] = None):
        """Setup the simulation scene instance.
        The function should be called in reset().
        """
        if scene_config is None:
            scene_config = self._get_default_scene_config()
        self._scene = self._engine.create_scene(scene_config)
        self._scene.set_timestep(1.0 / self._sim_freq)

    def _clear(self):
        """Clear the simulation scene instance and other buffers.
        The function can be called in reset() before a new scene is created.
        """
        self._close_viewer()
        self.agent = None
        self._cameras = OrderedDict()
        self._scene = None

    def close(self):
        self._clear()

    def _close_viewer(self):
        if self._viewer is None:
            return
        self._viewer.close()
        self._viewer = None

    # -------------------------------------------------------------------------- #
    # Simulation state (required for MPC)
    # -------------------------------------------------------------------------- #
    def get_actors(self):
        return self._scene.get_all_actors()

    def get_articulations(self):
        articulations = self._scene.get_all_articulations()
        # NOTE(jigu): There might be dummy articulations used by controllers.
        # TODO(jigu): Remove dummy articulations if exist.
        return articulations

    def get_sim_state(self) -> np.ndarray:
        """Get simulation state."""
        state = []
        for actor in self._actors:
            state.append(get_actor_state(actor))
        for articulation in self._articulations:
            state.append(get_articulation_state(articulation))
        return np.hstack(state)

    def set_sim_state(self, state: np.ndarray):
        """Set simulation state."""
        KINEMANTIC_DIM = 13  # [pos, quat, lin_vel, ang_vel]
        start = 0
        for actor in self._actors:
            set_actor_state(actor, state[start : start + KINEMANTIC_DIM])
            start += KINEMANTIC_DIM
        for articulation in self._articulations:
            ndim = KINEMANTIC_DIM + 2 * articulation.dof
            set_articulation_state(articulation, state[start : start + ndim])
            start += ndim

    def get_state(self):
        """Get environment state. Override to include task information (e.g., goal)"""
        return self.get_sim_state()

    def set_state(self, state: np.ndarray):
        """Set environment state. Override to include task information (e.g., goal)"""
        return self.set_sim_state(state)

    # -------------------------------------------------------------------------- #
    # Visualization
    # -------------------------------------------------------------------------- #
    _viewer: Viewer

    # Camera used for rendering only, should be created in `_setup_cameras`
    render_camera: sapien.CameraEntity

    def _setup_viewer(self):
        """Setup the interactive viewer.
        The function should be called in reset(), and overrided to adjust camera.
        """
        # CAUTION: call `set_scene` after assets are loaded.
        self._viewer.set_scene(self._scene)
        self._viewer.toggle_axes(False)
        self._viewer.toggle_camera_lines(False)

    def update_render(self):
        """Update renderer(s). This function should be called before any rendering,
        to sync simulator and renderer."""
        self._scene.update_render()

    def render(self, mode="human", **kwargs):
        self.update_render()
        if mode == "human":
            if self._viewer is None:
                self._viewer = Viewer(self._renderer)
                self._setup_viewer()
            self._viewer.render()
            return self._viewer
        elif mode == "rgb_array":
            self._cameras["render_camera"].take_picture()
            rgb = self._cameras["render_camera"].get_images()["Color"][..., :3]
            rgb = np.clip(rgb * 255, 0, 255).astype(np.uint8)
            return rgb
            self.render_camera.take_picture()
            return get_camera_rgb(self.render_camera)
        elif mode == "cameras":
            images = [self.render("rgb_array")]
            # NOTE(jigu): Must update renderer again
            # since some visual-only sites like goals should be hidden.
            self.update_render()

            # # Overwrite options if using GT segmentation
            # if self._enable_gt_seg:
            #     kwargs.update(visual_seg=True, actor_seg=True)

            # # Overwrite options if using KuaFu renderer
            # if self._enable_kuafu:
            #     kwargs.update(depth=False, visual_seg=False, actor_seg=False)

            # cameras_images = self._get_obs_images(**kwargs)
            # to_uint8 = lambda x: np.clip(x * 255, 0, 255).astype(np.uint8)
            cameras_images = {
                name: cam.get_images(take_picture=True)
                for name, cam in self._cameras.items()
                if name != "render_camera"
            }
            for camera_images in cameras_images.values():
                images.extend(observations_to_images(camera_images))
            return tile_images(images)
        else:
            raise NotImplementedError(f"Unsupported render mode {mode}.")

    def gen_scene_pcd(self, num_points: int = int(1e5)) -> np.ndarray:
        """Generate scene point cloud for motion planning, excluding the robot"""
        meshes = []
        articulations = self._scene.get_all_articulations()
        if self.agent is not None:
            articulations.pop(articulations.index(self.agent.robot))
        for articulation in articulations:
            articulation_mesh = merge_meshes(get_articulation_meshes(articulation))
            if articulation_mesh:
                meshes.append(articulation_mesh)

        for actor in self._scene.get_all_actors():
            actor_mesh = merge_meshes(get_actor_meshes(actor))
            if actor_mesh:
                meshes.append(
                    actor_mesh.apply_transform(
                        actor.get_pose().to_transformation_matrix()
                    )
                )

        scene_mesh = merge_meshes(meshes)
        scene_pcd = scene_mesh.sample(num_points)
        return scene_pcd
