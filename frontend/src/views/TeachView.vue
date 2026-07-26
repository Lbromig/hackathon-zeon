<script setup lang="ts">
// The teach tab, unchanged: `TeachPanel` and its children are another slice's files and are
// mounted here as-is. The waypoint checklist lives inside it; there is no separate checklist
// component any more.
import TeachPanel from "../components/teach/TeachPanel.vue";
</script>

<template>
  <TeachPanel />
</template>
