/** @type {import('next').NextConfig} */
const nextConfig = {
  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
  // `standalone` es solo para la imagen Docker. El smoke gate usa `next start`
  // local y en Windows el output standalone crea symlinks que dan EPERM, así
  // que lo desactivamos para ese build. Dev/CI/Docker siguen en standalone.
  output: process.env.NEXT_DISABLE_STANDALONE === "1" ? undefined : "standalone",

  // El smoke gate (playwright.smoke.config.ts) construye en un distDir aparte
  // para no colisionar con el `.next` del `next dev` en :3000 (la colisión
  // corrompía el PackFileCache de webpack y reventaba el build en WasmHash).
  // Por defecto sigue siendo `.next`, así que dev/CI no cambian.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  webpack: (config) => {
    // Build determinista para el gate: sin cache persistente no hay corrupción.
    if (process.env.NEXT_DISABLE_WEBPACK_CACHE === "1") {
      config.cache = false;
    }
    return config;
  },

  /**
   * Proxy reverso: todas las llamadas /api/* del frontend se redirigen
   * al servicio pipeline-api en la red interna de Docker.
   * Esto elimina la necesidad de NEXT_PUBLIC_API_URL y permite cambiar
   * la IP del servidor sin reconstruir la imagen.
   */
  async rewrites() {
    const apiBase = process.env.PIPELINE_API_URL ?? "http://pipeline-api:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
