/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  outputFileTracingRoot: require('path').join(__dirname, '../..'),
  transpilePackages: ['@eljazari/shared-types'],
  typescript: {
    // Check types in both CI and production builds.
    ignoreBuildErrors: false,
  },
  eslint: {
    // ESLint is run in the dedicated CI job; don't re-run during Docker build
    ignoreDuringBuilds: true,
  },
  experimental: {
    cpus: 2,
  },

  webpack: (config, { isServer }) => {
    // ── Monaco Editor workers ──────────────────────────────────────────────
    // Required for Monaco to work in Next.js — routes worker imports to
    // the MonacoWebpackPlugin-style self-contained worker bundles.
    if (!isServer) {
      config.module.rules.push({
        test: /\.ttf$/,
        type: 'asset/resource',
      })
    }

    // Prevent monaco-editor from loading in SSR
    if (isServer) {
      config.externals = [
        ...(Array.isArray(config.externals) ? config.externals : []),
        'monaco-editor',
      ]
    }

    // ── Resolve monaco-editor workers via MonacoWebpackPlugin ─────────────
    // Uses the pre-built workers shipped with monaco-editor package.
    if (!isServer) {
      const MonacoWebpackPlugin = (() => {
        try {
          return require('monaco-editor-webpack-plugin')
        } catch {
          return null
        }
      })()

      if (MonacoWebpackPlugin) {
        config.plugins.push(
          new MonacoWebpackPlugin({
            languages: ['python', 'json', 'markdown'],
            filename: 'static/[name].worker.js',
          })
        )
      }
    }

    return config
  },
}

module.exports = nextConfig
