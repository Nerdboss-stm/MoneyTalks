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
  minMs: 240,
  maxMs: 360,
  min: 0.24,
  max: 0.36,
} as const;

export const GRID = 4;
export const RULE_PX = 1;
export const MONO_CAPS_TRACKING = 0.04;
export const LINE_HEIGHT = 1.4;
export const MIN_TEXT_PX = 10;
