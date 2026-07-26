// Four tabs, one route each. Workflow is the default: it is the tab you watch a run from,
// and R-UI-1 makes it the answer to "what is happening now".
//
// Routes rather than the previous `localStorage` tab index, so a run can be linked to and a
// reload lands where you were. Each view is unmounted on navigation, which is what stops the
// teach poller and drops the MJPEG connections when you leave those tabs.
import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

import WorkflowView from "./views/WorkflowView.vue";
import TeachView from "./views/TeachView.vue";
import CamerasView from "./views/CamerasView.vue";
import LogsView from "./views/LogsView.vue";

export const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/workflow" },
  { path: "/workflow", name: "workflow", component: WorkflowView, meta: { label: "Workflow" } },
  { path: "/teach", name: "teach", component: TeachView, meta: { label: "Teach" } },
  { path: "/cameras", name: "cameras", component: CamerasView, meta: { label: "Cameras" } },
  { path: "/logs", name: "logs", component: LogsView, meta: { label: "Logs" } },
  // An unknown path lands on the workflow rather than on nothing.
  { path: "/:pathMatch(.*)*", redirect: "/workflow" },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

export default router;
