<script setup lang="ts">
import { onMounted, ref } from "vue";

const emit = defineEmits<{
  "open-tab": [tab: "fleet" | "cameras"];
}>();

const reel = ref<HTMLVideoElement | null>(null);
const playing = ref(false);
const mediaBase = import.meta.env.BASE_URL + "media/";
const reelUrl = mediaBase + "zeon-demo-reel.mp4";
const posterUrl = mediaBase + "zeon-demo-poster.jpg";

const steps = [
  { number: "01", title: "Uncap", detail: "Coordinated dual-arm manipulation" },
  { number: "02", title: "Transport", detail: "Secure the open sample tube" },
  { number: "03", title: "Present", detail: "Align beneath the pipette tip" },
  { number: "04", title: "Aspirate", detail: "Execute and verify liquid handling" },
];

onMounted(() => {
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    void reel.value?.play().catch(() => undefined);
  }
});
</script>

<template>
  <section class="demo-page">
    <article class="demo-reel-shell" :class="{ 'is-playing': playing }">
      <video
        ref="reel"
        class="demo-reel"
        muted
        loop
        playsinline
        controls
        preload="metadata"
        width="1280"
        height="720"
        :poster="posterUrl"
        aria-describedby="demo-reel-description"
        @play="playing = true"
        @pause="playing = false"
      >
        <source :src="reelUrl" type="video/mp4" />
        Your browser cannot play the Zeon demo reel.
      </video>

      <div class="demo-reel-shade" aria-hidden="true" />
      <div class="demo-reel-copy">
        <span>05 · DEMO REEL / 00:52</span>
        <h2>See. Act. <em>Verify.</em></h2>
        <p id="demo-reel-description">
          A dual-arm wet-lab sequence combining camera fusion, coordinated manipulation,
          and liquid handling. Soundtrack only; no dialogue.
        </p>
        <div class="demo-reel-tags" aria-label="Demo attributes">
          <i>DUAL ARM</i>
          <i>3 CAMERAS</i>
          <i>OT-ONE</i>
          <i>52 SEC</i>
        </div>
      </div>

      <a class="demo-download" :href="reelUrl" download>
        DOWNLOAD REEL <b>↓</b>
      </a>
    </article>

    <div class="demo-strip" aria-label="System summary">
      <span><b>02</b> ROBOT ARMS</span>
      <span><b>03</b> VISION NODES</span>
      <span><b>01</b> LIQUID HANDLER</span>
      <span><b>04</b> VERIFIED STAGES</span>
    </div>

    <section class="demo-sequence" aria-labelledby="demo-sequence-title">
      <div class="demo-section-heading">
        <div>
          <span>MISSION LOGIC</span>
          <h2 id="demo-sequence-title">Uncap → Aspirate</h2>
        </div>
        <p>One visual language from physical bench to operator interface.</p>
      </div>

      <ol>
        <li v-for="step in steps" :key="step.number">
          <span>{{ step.number }}</span>
          <strong>{{ step.title }}</strong>
          <p>{{ step.detail }}</p>
          <i aria-hidden="true">↗</i>
        </li>
      </ol>
    </section>

    <section class="demo-brand-grid">
      <article class="demo-brand-card">
        <span class="demo-eyebrow">BRAND SYSTEM · ZEON</span>
        <h2>Built for the bench.<br />Framed for the demo.</h2>
        <p>
          Carbon surfaces keep the hardware legible. Signal lime marks action and
          verified state. Warm orange is reserved for faults and operator attention.
        </p>
        <div class="demo-swatches" aria-label="Zeon brand colors">
          <span class="swatch-lime"><i />D5FF3F · ACTION</span>
          <span class="swatch-carbon"><i />090B0A · SURFACE</span>
          <span class="swatch-signal"><i />FF8D62 · SIGNAL</span>
        </div>
      </article>

      <article class="demo-cue-card">
        <span class="demo-eyebrow">PRESENTER CUE · 01</span>
        <blockquote>
          “Zeon turns a fragile handoff between robots into one observable,
          verification-first laboratory workflow.”
        </blockquote>
        <div class="demo-cue-actions">
          <button @click="emit('open-tab', 'fleet')">Open control surface <b>↗</b></button>
          <button @click="emit('open-tab', 'cameras')">Open vision system <b>↗</b></button>
        </div>
      </article>
    </section>
  </section>
</template>

<style scoped>
.demo-page {
  display: grid;
  gap: 16px;
}

.demo-reel-shell {
  position: relative;
  min-height: min(68vh, 760px);
  overflow: hidden;
  border: 1px solid var(--zeon-line);
  background: #050706;
  isolation: isolate;
}

.demo-reel-shell::before {
  content: "";
  position: absolute;
  z-index: 3;
  inset: 0;
  pointer-events: none;
  border: 1px solid rgba(213, 255, 63, 0.08);
  background: repeating-linear-gradient(
    to bottom,
    transparent 0,
    transparent 3px,
    rgba(255, 255, 255, 0.012) 4px
  );
}

.demo-reel {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #050706;
}

.demo-reel-shade {
  position: absolute;
  z-index: 1;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, rgba(5, 8, 6, 0.96) 0%, rgba(5, 8, 6, 0.66) 34%, transparent 72%),
    linear-gradient(0deg, rgba(5, 8, 6, 0.84) 0%, transparent 34%);
  transition: opacity 240ms ease;
}

.demo-reel-copy {
  position: absolute;
  z-index: 2;
  left: clamp(24px, 5vw, 76px);
  bottom: clamp(72px, 10vh, 112px);
  width: min(690px, 70%);
  pointer-events: none;
  transition: opacity 240ms ease;
}

.demo-reel-shell.is-playing .demo-reel-copy,
.demo-reel-shell.is-playing .demo-reel-shade {
  opacity: 0;
}

.demo-reel-copy > span,
.demo-eyebrow,
.demo-section-heading span {
  color: var(--zeon-lime);
  font: 750 9px/1 "SFMono-Regular", Consolas, monospace;
  letter-spacing: 0.16em;
}

.demo-reel-copy h2 {
  margin: 18px 0 16px;
  color: var(--zeon-text);
  font-size: clamp(54px, 7.5vw, 118px);
  font-weight: 850;
  line-height: 0.86;
  letter-spacing: -0.075em;
  text-transform: uppercase;
}

.demo-reel-copy h2 em {
  color: transparent;
  font-style: normal;
  -webkit-text-stroke: 1px rgba(213, 255, 63, 0.7);
}

.demo-reel-copy p {
  max-width: 590px;
  margin: 0;
  color: #b8c0b8;
  font-size: 13px;
  line-height: 1.55;
}

.demo-reel-tags {
  margin-top: 22px;
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.demo-reel-tags i {
  padding: 7px 9px;
  border: 1px solid rgba(213, 255, 63, 0.28);
  background: rgba(8, 11, 8, 0.68);
  color: var(--zeon-lime);
  font: 750 8px/1 "SFMono-Regular", Consolas, monospace;
  font-style: normal;
  letter-spacing: 0.08em;
}

.demo-download {
  position: absolute;
  z-index: 4;
  right: 22px;
  top: 22px;
  min-width: 150px;
  height: 40px;
  padding: 0 8px 0 13px;
  border: 1px solid rgba(213, 255, 63, 0.4);
  background: rgba(8, 11, 8, 0.78);
  color: var(--zeon-lime);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font: 750 8px/1 "SFMono-Regular", Consolas, monospace;
  letter-spacing: 0.08em;
  text-decoration: none;
}

.demo-download:hover {
  background: var(--zeon-lime);
  color: #080b08;
}

.demo-strip {
  min-height: 66px;
  border: 1px solid var(--zeon-line);
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  background: rgba(12, 15, 13, 0.86);
}

.demo-strip span {
  padding: 0 20px;
  border-right: 1px solid var(--zeon-line);
  color: #7d857e;
  display: flex;
  align-items: center;
  gap: 12px;
  font: 700 8px/1 "SFMono-Regular", Consolas, monospace;
  letter-spacing: 0.08em;
}

.demo-strip span:last-child {
  border: 0;
}

.demo-strip b {
  color: var(--zeon-lime);
  font-size: 16px;
}

.demo-sequence,
.demo-brand-card,
.demo-cue-card {
  border: 1px solid var(--zeon-line);
  background: rgba(11, 14, 12, 0.88);
}

.demo-sequence {
  padding: 24px;
}

.demo-section-heading {
  margin-bottom: 22px;
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
}

.demo-section-heading h2,
.demo-brand-card h2 {
  margin: 8px 0 0;
  color: var(--zeon-text);
  font-size: 28px;
  line-height: 1;
  letter-spacing: -0.04em;
}

.demo-section-heading p {
  max-width: 380px;
  margin: 0;
  color: var(--zeon-muted);
  font-size: 11px;
  line-height: 1.5;
  text-align: right;
}

.demo-sequence ol {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid rgba(213, 255, 63, 0.28);
}

.demo-sequence li {
  position: relative;
  min-height: 158px;
  padding: 20px;
  border-right: 1px solid var(--zeon-line);
  display: flex;
  flex-direction: column;
}

.demo-sequence li:last-child {
  border: 0;
}

.demo-sequence li > span {
  color: var(--zeon-lime);
  font: 750 9px/1 "SFMono-Regular", Consolas, monospace;
}

.demo-sequence li strong {
  margin-top: 30px;
  color: var(--zeon-text);
  font-size: 17px;
  text-transform: uppercase;
}

.demo-sequence li p {
  max-width: 190px;
  margin: 8px 0 0;
  color: var(--zeon-muted);
  font-size: 10px;
  line-height: 1.4;
}

.demo-sequence li > i {
  position: absolute;
  right: 16px;
  top: 18px;
  color: #565e57;
  font-style: normal;
}

.demo-brand-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(340px, 0.85fr);
  gap: 16px;
}

.demo-brand-card,
.demo-cue-card {
  min-height: 300px;
  padding: 26px;
}

.demo-brand-card p {
  max-width: 650px;
  margin: 18px 0 0;
  color: var(--zeon-muted);
  font-size: 11px;
  line-height: 1.6;
}

.demo-swatches {
  margin-top: 30px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.demo-swatches span {
  min-height: 76px;
  padding: 12px;
  border: 1px solid var(--zeon-line);
  color: #8d958e;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  font: 700 7px/1 "SFMono-Regular", Consolas, monospace;
}

.demo-swatches i {
  width: 22px;
  height: 22px;
  display: block;
  border: 1px solid rgba(255, 255, 255, 0.16);
}

.swatch-lime i { background: #d5ff3f; }
.swatch-carbon i { background: #090b0a; }
.swatch-signal i { background: #ff8d62; }

.demo-cue-card {
  display: flex;
  flex-direction: column;
}

.demo-cue-card blockquote {
  margin: 24px 0;
  color: var(--zeon-text);
  font-size: clamp(20px, 2vw, 31px);
  font-weight: 600;
  line-height: 1.18;
  letter-spacing: -0.035em;
}

.demo-cue-actions {
  margin-top: auto;
  display: flex;
  gap: 8px;
}

.demo-cue-actions button {
  min-height: 42px;
  padding: 0 13px;
  border: 1px solid rgba(213, 255, 63, 0.3);
  background: transparent;
  color: var(--zeon-lime);
  cursor: pointer;
  font: 720 8px/1 "SFMono-Regular", Consolas, monospace;
  text-transform: uppercase;
}

.demo-cue-actions button:hover {
  background: var(--zeon-lime);
  color: #080b08;
}

@media (max-width: 1000px) {
  .demo-reel-shell {
    min-height: 560px;
  }

  .demo-strip,
  .demo-sequence ol {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .demo-strip span:nth-child(2),
  .demo-sequence li:nth-child(2) {
    border-right: 0;
  }

  .demo-strip span:nth-child(-n + 2) {
    border-bottom: 1px solid var(--zeon-line);
  }

  .demo-brand-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 680px) {
  .demo-reel-shell {
    min-height: 510px;
  }

  .demo-reel-copy {
    left: 20px;
    bottom: 74px;
    width: calc(100% - 40px);
  }

  .demo-reel-copy h2 {
    font-size: clamp(46px, 15vw, 68px);
  }

  .demo-reel-copy p {
    display: none;
  }

  .demo-download {
    right: 12px;
    top: 12px;
  }

  .demo-strip,
  .demo-sequence ol,
  .demo-swatches {
    grid-template-columns: 1fr;
  }

  .demo-strip span,
  .demo-sequence li,
  .demo-sequence li:nth-child(2) {
    border-right: 0;
    border-bottom: 1px solid var(--zeon-line);
  }

  .demo-section-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .demo-section-heading p {
    text-align: left;
  }

  .demo-cue-actions {
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .demo-reel-shell::before {
    display: none;
  }
}
</style>
