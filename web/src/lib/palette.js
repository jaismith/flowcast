// The page's colors, read by Plot and MapLibre. The same values are the --color-* variables in src/index.css,
// which Tailwind's utilities use; keep the two in step.

const FLOOD = { action: '#f2c200', minor: '#f08c00', moderate: '#e03131', major: '#ae3ec9' };

export const C = {
  paper: '#f6f5f1',
  card: '#ffffff',
  ink: '#16191d',
  muted: '#6b7079',
  faint: '#a3a6ad',
  line: '#e6e3dc',
  normal: '#ece8df',
  flow: '#1f4e79',
  rain: '#3d7fe8',
  snow: '#a48bf0',
  melt: '#26a69a',
  sun: '#f0a202',
  alert: '#d9480f',
  ...FLOOD,
};
