import type { Metadata } from 'next'
import { Inter, Cairo } from 'next/font/google'
import './globals.css'
import AppBootstrap from './AppBootstrap'
import { Providers } from './providers'

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
})

const cairo = Cairo({
  subsets: ['arabic', 'latin'],
  variable: '--font-cairo',
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'eljazari.ai — Build & Test a Drone from a Prompt',
  description:
    'Transform a natural language description into a fully designed, coded, simulated, and tested drone — end-to-end, with AI agents.',
  keywords: [
    'drone', 'AI', 'UAV', 'PX4', 'MAVSDK', 'simulation', 'LangGraph',
    'autonomous', 'URDF', 'Gazebo',
  ],
  authors: [{ name: 'eljazari.ai' }],
  creator: 'eljazari.ai',
  openGraph: {
    type: 'website',
    locale: 'en_US',
    alternateLocale: 'ar_SA',
    url: 'https://eljazari.ai',
    siteName: 'eljazari.ai',
    title: 'eljazari.ai — Build & Test a Drone from a Prompt',
    description:
      'AI-powered multi-agent platform: design, code, validate, simulate, and evaluate drones from a single text prompt.',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'eljazari.ai — Build & Test a Drone from a Prompt',
    description:
      'AI-powered multi-agent platform: design, code, validate, simulate, and evaluate drones from a single text prompt.',
  },
  icons: {
    icon: '/favicon.png',
  },
  robots: {
    index: true,
    follow: true,
  },
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" dir="ltr" suppressHydrationWarning className={`${inter.variable} ${cairo.variable}`}>
      <body className="bg-background text-foreground antialiased font-sans" suppressHydrationWarning>
        <Providers>
          <AppBootstrap />
          {children}
        </Providers>
      </body>
    </html>
  )
}
