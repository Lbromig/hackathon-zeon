<script setup lang="ts">
// One artifact: an overlay or a frame, served through the run's artifact route.
//
// Never loaded from `Artifact.path`: that is an absolute filesystem path today (review
// non-blocking 4), and a client-supplied path is a traversal. `artifactUrl()` reduces it to a
// basename under the run. A JSON artifact is a link, not an image.
import { computed, ref } from "vue";
import { artifactUrl, type Artifact } from "../../api/engine";

const props = defineProps<{ artifact: Artifact; runId: string }>();

const failed = ref(false);
const url = computed(() => artifactUrl(props.runId, props.artifact));
const isImage = computed(() => props.artifact.kind.startsWith("image/"));
const caption = computed(() =>
  [props.artifact.label, props.artifact.camera, props.artifact.kind]
    .filter(Boolean).join(" · "));
</script>

<template>
  <figure class="rounded-lg border border-deck-600 bg-deck-900 p-1.5">
    <a v-if="url && isImage && !failed" :href="url" target="_blank" rel="noopener">
      <img :src="url" :alt="caption" class="block w-full rounded" @error="failed = true" />
    </a>
    <a v-else-if="url" :href="url" target="_blank" rel="noopener" class="block px-1 py-2 text-xs text-blue-300 underline">
      {{ failed ? "image did not load — open it directly" : "open artifact" }}
    </a>
    <p v-else class="px-1 py-2 text-xs text-amber-300">
      no URL for this artifact — the record carries only a path
      (<span class="num">{{ artifact.path }}</span>)
    </p>
    <figcaption class="num mt-1 truncate px-1 text-[11px] text-deck-400" :title="artifact.path">
      {{ caption }}
    </figcaption>
  </figure>
</template>
