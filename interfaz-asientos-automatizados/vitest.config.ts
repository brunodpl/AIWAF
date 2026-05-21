import { defineConfig } from 'vitest/config';
import path from 'path';

export default defineConfig({
  // Los tests .tsx (componentes) se compilan con el runtime JSX automático.
  // El tsconfig de Next usa "jsx": "preserve" (lo resuelve swc en build); Vitest 4
  // transforma con oxc, que por defecto hereda ese "preserve" y deja el JSX intacto
  // (el parser SSR luego falla). Forzamos el runtime automático para los .tsx.
  oxc: { jsx: { runtime: 'automatic', importSource: 'react' } },
  test: {
    environment: 'jsdom',
    globals: true,
    include: [
      'lib/**/*.test.ts',
      'lib/**/*.test.tsx',
      'components/**/*.test.tsx',
    ],
    exclude: ['e2e/**', 'node_modules/**'],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './'),
    },
  },
});
