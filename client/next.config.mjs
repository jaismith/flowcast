/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  swcMinify: true,
  output: 'standalone',
  redirects: () => ([
    {
      source: '/',
      destination: '/sites/01427510',
      permanent: false
    }
  ])
};

export default nextConfig;
