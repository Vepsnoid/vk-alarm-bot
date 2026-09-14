/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', 'SF Pro Display', 'SF Pro Text', 'Inter', 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
      },
      colors: {
        // Apple-style accent blue (macOS/iOS system blue family)
        primary: { 50: '#eef6ff', 100: '#d9ecff', 200: '#b8dcff', 300: '#85c4ff', 400: '#4aa8ff', 500: '#0071e3', 600: '#0062c4', 700: '#00509f', 800: '#003f7d', 900: '#00305f', 950: '#001c38' },
        accent: { 50: '#eff6ff', 100: '#dbeafe', 200: '#bfdbfe', 300: '#93c5fd', 400: '#60a5fa', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8', 800: '#1e40af', 900: '#1e3a8a' },
        surface: { 50: '#fbfbfd', 100: '#f5f5f7', 200: '#e8e8ed', 300: '#d2d2d7', 400: '#aeaeb2', 500: '#8e8e93', 600: '#636366', 700: '#48484a', 800: '#2c2c2e', 900: '#1c1c1e' }
      },
      borderRadius: { '2xl': '1rem', '3xl': '1.375rem' },
      boxShadow: {
        soft: '0 1px 2px rgba(0,0,0,0.04), 0 8px 24px -12px rgba(0,0,0,0.18)',
        card: '0 1px 2px rgba(0,0,0,0.04), 0 14px 34px -16px rgba(0,0,0,0.22)',
      }
    }
  },
  plugins: []
}