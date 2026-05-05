/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The screener is read-only against a hosted Postgres. Long-running
  // socket pools die in serverless: we use Neon's HTTP driver instead.
  // Next 15+ moved this option out of `experimental` to the top level.
  serverExternalPackages: ["@neondatabase/serverless"],
};

export default nextConfig;
