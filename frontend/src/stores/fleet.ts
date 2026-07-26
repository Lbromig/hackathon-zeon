// The `/ws/state` fleet socket is opened once, in `App.vue`, and shared by injection.
//
// One socket per app, not one per view: the camera tab needs the per-camera detections and the
// header needs the connection state, and two sockets carrying the same message would make the
// two disagree during a reconnect.
import type { InjectionKey, Ref } from "vue";
import type { CameraFrameState } from "../api/cameras";

export const CamerasKey: InjectionKey<Ref<Record<string, CameraFrameState>>> =
  Symbol("fleet.cameras");
