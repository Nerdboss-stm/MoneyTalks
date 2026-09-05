export const COLOR = {
  bg: 0x0b0c0e,
  text: 0xe6e6e3,
  muted: 0x7c8087,
  rule: 0x2a2c30,
  amber: 0xd89b2b,
  red: 0xd23b3b,
} as const;

export const CSS = {
  bg: "#0B0C0E",
  text: "#E6E6E3",
  muted: "#7C8087",
  rule: "#2A2C30",
  amber: "#D89B2B",
  red: "#D23B3B",
} as const;

export const FONT = {
  mono: '"IBM Plex Mono", monospace',
  sans: '"Inter Tight", sans-serif',
} as const;

export const MOTION = {
  ease: "power2.out",
  fast: 0.24,
  slow: 0.36,
  drop: 0.32,
  stagger: 0.08,
} as const;

export const GRID = 4;
export const RULE_PX = 1;
export const MONO_CAPS_TRACKING = 0.04;
export const LINE_HEIGHT = 1.4;
export const MIN_TEXT_PX = 10;

// Radial round table. C = (50vw, 47vh), R = 0.40 * min(vw, vh).
export const RADIAL = {
  cx: 0.5,
  cy: 0.47,
  r: 0.4,
  labelR: 0.28, // agent name ring
  farR: 0.35, // 5 company days out
  holdR: 0.55, // held / escalated park radius
  minutesAtRing: 5,
  daysAtFar: 5,
  barMin: 16,
  barMax: 72,
  tickGap: 6, // executed tick starts this far outside the ring
  tickLen: 8,
  tagGap: 8,
  push: 12, // collision offset, outward
} as const;

export const snap = (v: number, g: number = GRID): number => Math.round(v / g) * g;
