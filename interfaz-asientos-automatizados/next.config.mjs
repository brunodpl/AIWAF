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
  output: "standalone",

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
