/** @type {import('next').NextConfig} */
// get values from environment variables
const { DASHBOARD_SERVER_PORT } = process.env

const nextConfig = {
    rewrites: async () => {
        return [
            {
                source: '/',
                destination: `/recordings`,
            },
            {
                source: '/api/:path*',
                destination: `http://127.0.0.1:${DASHBOARD_SERVER_PORT}/api/:path*`
            },
            {
                source: '/docs',
                destination:
                    process.env.NODE_ENV === 'development'
                        ? `http://127.0.0.1:${DASHBOARD_SERVER_PORT}/docs`
                        : '/api/docs',
            },
            {
                source: '/openapi.json',
                destination:
                    process.env.NODE_ENV === 'development'
                        ? `http://127.0.0.1:${DASHBOARD_SERVER_PORT}/openapi.json`
                        : '/api/openapi.json',
            },
        ]
    },
    output: 'export',
    reactStrictMode: false,
}

module.exports = nextConfig
