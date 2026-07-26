<script setup lang="ts">
// The camera tab, unchanged. The per-camera detection states come from the one `/ws/state`
// socket App.vue owns, injected rather than re-opened — see `stores/fleet.ts`.
import { inject, ref } from "vue";
import CameraTab from "../components/cameras/CameraTab.vue";
import { CamerasKey } from "../stores/fleet";

const cameras = inject(CamerasKey, ref({}));
</script>

<template>
  <CameraTab :cameras="cameras" />
</template>
