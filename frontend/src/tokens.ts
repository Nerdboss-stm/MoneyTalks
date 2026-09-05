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

export const DESIGN_W = 1920;
export const DESIGN_H = 1080;

export const LAYOUT = {
  lineX: 1496, // 78% of 1920, snapped to 4px
  leftX: 640, // right edge of a payment 5 company days out; leaves room for a 180px bar + 380px tag
  laneTop: 88,
  laneH: 48,
  holdY: 668, // 62% of 1080, snapped
  escX: 1520,
  escTop: 96,
  escPitch: 36, // bar + two tag rows + 6px
  stackPitch: 18,
  labelY: 1040,
  rulerStep: 60,
  minutesAtLine: 5,
  daysAtLeft: 5,
} as const;

export const snap = (v: number, g: number = GRID): number => Math.round(v / g) * g;
