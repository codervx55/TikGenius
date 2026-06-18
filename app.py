
# Create the complete modern frontend system for TikGenius
# This will include all HTML templates, CSS, and JavaScript

import os

output_dir = "/mnt/agents/output/tikgenius_frontend"
os.makedirs(output_dir, exist_ok=True)
os.makedirs(f"{output_dir}/templates", exist_ok=True)
os.makedirs(f"{output_dir}/static/css", exist_ok=True)
os.makedirs(f"{output_dir}/static/js", exist_ok=True)

# ============================================
# 1. BASE CSS - Modern Design System
# ============================================
base_css = '''/* ============================================
   TIKGENIUS DESIGN SYSTEM v2.0
   Modern, Mobile-First, Accessible
   ============================================ */

/* CSS Custom Properties (Design Tokens) */
:root {
  /* Colors - OKLCH for perceptually uniform colors */
  --primary: oklch(65% 0.18 25);
  --primary-hover: oklch(58% 0.2 25);
  --primary-muted: oklch(65% 0.05 25);
  --primary-ghost: oklch(65% 0.18 25 / 0.08);
  
  --surface: oklch(98% 0.005 250);
  --surface-elevated: oklch(100% 0 0);
  --surface-sunken: oklch(95% 0.01 250);
  
  --text: oklch(20% 0.02 250);
  --text-secondary: oklch(45% 0.02 250);
  --text-muted: oklch(60% 0.01 250);
  --text-inverse: oklch(98% 0.005 250);
  
  --border: oklch(85% 0.01 250);
  --border-subtle: oklch(90% 0.005 250);
  
  --success: oklch(65% 0.15 145);
  --success-bg: oklch(65% 0.15 145 / 0.1);
  --warning: oklch(75% 0.15 85);
  --warning-bg: oklch(75% 0.15 85 / 0.1);
  --error: oklch(60% 0.2 25);
  --error-bg: oklch(60% 0.2 25 / 0.1);
  --info: oklch(60% 0.15 250);
  --info-bg: oklch(60% 0.15 250 / 0.1);
  
  /* Typography */
  --font-sans: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
  
  --text-xs: clamp(0.625rem, 1.2vw, 0.75rem);
  --text-sm: clamp(0.75rem, 1.5vw, 0.875rem);
  --text-base: clamp(0.875rem, 1.8vw, 1rem);
  --text-lg: clamp(1rem, 2.2vw, 1.125rem);
  --text-xl: clamp(1.25rem, 3vw, 1.5rem);
  --text-2xl: clamp(1.5rem, 4vw, 2rem);
  --text-3xl: clamp(2rem, 5vw, 3rem);
  
  --font-normal: 400;
  --font-medium: 500;
  --font-semibold: 600;
  --font-bold: 700;
  
  /* Spacing */
  --space-1: clamp(0.25rem, 0.5vw, 0.375rem);
  --space-2: clamp(0.5rem, 1vw, 0.75rem);
  --space-3: clamp(0.75rem, 1.5vw, 1rem);
  --space-4: clamp(1rem, 2vw, 1.5rem);
  --space-5: clamp(1.5rem, 3vw, 2rem);
  --space-6: clamp(2rem, 4vw, 3rem);
  --space-8: clamp(3rem, 6vw, 4rem);
  --space-10: clamp(4rem, 8vw, 6rem);
  
  /* Layout */
  --radius-sm: 0.375rem;
  --radius-md: 0.625rem;
  --radius-lg: 1rem;
  --radius-xl: 1.5rem;
  --radius-full: 9999px;
  
  /* Shadows */
  --shadow-sm: 0 1px 2px 0 oklch(0% 0 0 / 0.05);
  --shadow-md: 0 4px 6px -1px oklch(0% 0 0 / 0.1), 0 2px 4px -2px oklch(0% 0 0 / 0.1);
  --shadow-lg: 0 10px 15px -3px oklch(0% 0 0 / 0.1), 0 4px 6px -4px oklch(0% 0 0 / 0.1);
  --shadow-xl: 0 20px 25px -5px oklch(0% 0 0 / 0.1), 0 8px 10px -6px oklch(0% 0 0 / 0.1);
  --shadow-glow: 0 0 20px oklch(65% 0.18 25 / 0.3);
  
  /* Transitions */
  --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-base: 250ms cubic-bezier(0.4, 0, 0.2, 1);
  --transition-slow: 350ms cubic-bezier(0.4, 0, 0.2, 1);
  
  /* Z-index scale */
  --z-base: 0;
  --z-dropdown: 100;
  --z-sticky: 200;
  --z-modal-backdrop: 300;
  --z-modal: 400;
  --z-tooltip: 500;
  --z-toast: 600;
}

/* Dark mode */
[data-theme="dark"] {
  --primary: oklch(70% 0.18 25);
  --primary-hover: oklch(75% 0.2 25);
  --primary-muted: oklch(70% 0.05 25);
  --primary-ghost: oklch(70% 0.18 25 / 0.12);
  
  --surface: oklch(18% 0.02 250);
  --surface-elevated: oklch(22% 0.02 250);
  --surface-sunken: oklch(14% 0.02 250);
  
  --text: oklch(95% 0.01 250);
  --text-secondary: oklch(75% 0.01 250);
  --text-muted: oklch(60% 0.01 250);
  --text-inverse: oklch(18% 0.02 250);
  
  --border: oklch(30% 0.02 250);
  --border-subtle: oklch(25% 0.01 250);
  
  --shadow-sm: 0 1px 2px 0 oklch(0% 0 0 / 0.3);
  --shadow-md: 0 4px 6px -1px oklch(0% 0 0 / 0.4), 0 2px 4px -2px oklch(0% 0 0 / 0.3);
  --shadow-lg: 0 10px 15px -3px oklch(0% 0 0 / 0.4), 0 4px 6px -4px oklch(0% 0 0 / 0.3);
  --shadow-xl: 0 20px 25px -5px oklch(0% 0 0 / 0.5), 0 8px 10px -6px oklch(0% 0 0 / 0.4);
}

/* ============================================
   RESET & BASE
   ============================================ */
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html {
  scroll-behavior: smooth;
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

body {
  font-family: var(--font-sans);
  font-size: var(--text-base);
  line-height: 1.6;
  color: var(--text);
  background: var(--surface);
  min-height: 100dvh;
  overflow-x: hidden;
  overflow-y: auto;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* Focus styles for accessibility */
:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

/* Selection */
::selection {
  background: var(--primary-ghost);
  color: var(--primary);
}

/* Scrollbar styling */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: var(--surface-sunken);
}

::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: var(--radius-full);
}

::-webkit-scrollbar-thumb:hover {
  background: var(--text-muted);
}

/* ============================================
   TYPOGRAPHY
   ============================================ */
h1, h2, h3, h4, h5, h6 {
  font-weight: var(--font-bold);
  line-height: 1.2;
  letter-spacing: -0.02em;
  color: var(--text);
}

h1 { font-size: var(--text-3xl); }
h2 { font-size: var(--text-2xl); }
h3 { font-size: var(--text-xl); }
h4 { font-size: var(--text-lg); }
h5, h6 { font-size: var(--text-base); }

p {
  color: var(--text-secondary);
  max-width: 65ch;
}

a {
  color: var(--primary);
  text-decoration: none;
  transition: color var(--transition-fast);
}

a:hover {
  color: var(--primary-hover);
  text-decoration: underline;
}

.text-muted { color: var(--text-muted); }
.text-secondary { color: var(--text-secondary); }
.text-primary { color: var(--primary); }
.text-success { color: var(--success); }
.text-error { color: var(--error); }
.text-warning { color: var(--warning); }

.font-mono { font-family: var(--font-mono); }
.font-medium { font-weight: var(--font-medium); }
.font-semibold { font-weight: var(--font-semibold); }
.font-bold { font-weight: var(--font-bold); }

/* ============================================
   LAYOUT UTILITIES
   ============================================ */
.container {
  width: 100%;
  max-width: 1280px;
  margin: 0 auto;
  padding: 0 var(--space-4);
}

.container-narrow {
  max-width: 640px;
}

.container-wide {
  max-width: 1440px;
}

.section {
  padding: var(--space-8) 0;
}

.section-sm {
  padding: var(--space-5) 0;
}

.grid {
  display: grid;
  gap: var(--space-4);
}

.grid-2 {
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}

.grid-3 {
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

.grid-4 {
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.flex {
  display: flex;
}

.flex-col {
  flex-direction: column;
}

.items-center {
  align-items: center;
}

.justify-center {
  justify-content: center;
}

.justify-between {
  justify-content: space-between;
}

.gap-1 { gap: var(--space-1); }
.gap-2 { gap: var(--space-2); }
.gap-3 { gap: var(--space-3); }
.gap-4 { gap: var(--space-4); }
.gap-5 { gap: var(--space-5); }
.gap-6 { gap: var(--space-6); }

.w-full { width: 100%; }
.h-full { height: 100%; }

/* ============================================
   COMPONENTS
   ============================================ */

/* ---- Cards ---- */
.card {
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
  transition: transform var(--transition-base), box-shadow var(--transition-base);
}

.card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}

.card-elevated {
  box-shadow: var(--shadow-md);
  border: none;
}

.card-header {
  margin-bottom: var(--space-4);
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
}

.card-footer {
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px solid var(--border-subtle);
}

/* ---- Buttons ---- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  line-height: 1;
  border-radius: var(--radius-md);
  border: 2px solid transparent;
  cursor: pointer;
  transition: all var(--transition-fast);
  min-height: 48px;
  min-width: 48px;
  text-decoration: none;
  white-space: nowrap;
  position: relative;
  overflow: hidden;
}

.btn::after {
  content: '';
  position: absolute;
  inset: 0;
  background: oklch(100% 0 0 / 0);
  transition: background var(--transition-fast);
}

.btn:hover::after {
  background: oklch(100% 0 0 / 0.1);
}

.btn:active {
  transform: scale(0.97);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  pointer-events: none;
}

.btn-primary {
  background: var(--primary);
  color: var(--text-inverse);
  border-color: var(--primary);
}

.btn-primary:hover {
  background: var(--primary-hover);
  border-color: var(--primary-hover);
}

.btn-secondary {
  background: var(--surface-elevated);
  color: var(--text);
  border-color: var(--border);
}

.btn-secondary:hover {
  background: var(--surface-sunken);
  border-color: var(--text-muted);
}

.btn-ghost {
  background: transparent;
  color: var(--text);
  border-color: transparent;
}

.btn-ghost:hover {
  background: var(--primary-ghost);
  color: var(--primary);
}

.btn-danger {
  background: var(--error);
  color: var(--text-inverse);
  border-color: var(--error);
}

.btn-sm {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  min-height: 36px;
}

.btn-lg {
  padding: var(--space-4) var(--space-6);
  font-size: var(--text-base);
  min-height: 56px;
}

.btn-icon {
  padding: var(--space-3);
  border-radius: var(--radius-md);
}

/* ---- Forms ---- */
.form-group {
  margin-bottom: var(--space-4);
}

.form-label {
  display: block;
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--text);
  margin-bottom: var(--space-2);
}

.form-label .required::after {
  content: ' *';
  color: var(--error);
}

.form-hint {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin-top: var(--space-1);
}

.form-input,
.form-select,
.form-textarea {
  width: 100%;
  padding: var(--space-3) var(--space-4);
  font-family: var(--font-sans);
  font-size: var(--text-base);
  color: var(--text);
  background: var(--surface-elevated);
  border: 2px solid var(--border);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  min-height: 48px;
}

.form-input:hover,
.form-select:hover,
.form-textarea:hover {
  border-color: var(--text-muted);
}

.form-input:focus,
.form-select:focus,
.form-textarea:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-ghost);
  outline: none;
}

.form-input::placeholder,
.form-textarea::placeholder {
  color: var(--text-muted);
}

.form-input:disabled,
.form-select:disabled,
.form-textarea:disabled {
  background: var(--surface-sunken);
  cursor: not-allowed;
  opacity: 0.6;
}

.form-textarea {
  min-height: 120px;
  resize: vertical;
  line-height: 1.6;
}

.form-select {
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 12px center;
  padding-right: 40px;
}

.form-error {
  border-color: var(--error) !important;
  box-shadow: 0 0 0 3px var(--error-bg) !important;
}

.error-message {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--error);
  margin-top: var(--space-2);
}

/* ---- Badges ---- */
.badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  border-radius: var(--radius-full);
  line-height: 1;
}

.badge-primary {
  background: var(--primary-ghost);
  color: var(--primary);
}

.badge-success {
  background: var(--success-bg);
  color: var(--success);
}

.badge-warning {
  background: var(--warning-bg);
  color: var(--warning);
}

.badge-error {
  background: var(--error-bg);
  color: var(--error);
}

.badge-info {
  background: var(--info-bg);
  color: var(--info);
}

/* ---- Alerts ---- */
.alert {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-4);
  border-radius: var(--radius-lg);
  border: 1px solid;
  font-size: var(--text-sm);
}

.alert-success {
  background: var(--success-bg);
  border-color: var(--success);
  color: var(--success);
}

.alert-warning {
  background: var(--warning-bg);
  border-color: var(--warning);
  color: var(--warning);
}

.alert-error {
  background: var(--error-bg);
  border-color: var(--error);
  color: var(--error);
}

.alert-info {
  background: var(--info-bg);
  border-color: var(--info);
  color: var(--info);
}

.alert-icon {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
}

.alert-content {
  flex: 1;
}

.alert-title {
  font-weight: var(--font-semibold);
  margin-bottom: var(--space-1);
}

/* ---- Navigation ---- */
.navbar {
  position: sticky;
  top: 0;
  z-index: var(--z-sticky);
  background: oklch(100% 0 0 / 0.8);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border-subtle);
}

[data-theme="dark"] .navbar {
  background: oklch(18% 0.02 250 / 0.8);
}

.navbar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 64px;
  padding: 0 var(--space-4);
}

.navbar-brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  color: var(--text);
  text-decoration: none;
}

.navbar-brand:hover {
  color: var(--primary);
  text-decoration: none;
}

.navbar-brand .brand-icon {
  width: 36px;
  height: 36px;
  background: var(--primary);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-inverse);
}

.navbar-nav {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.nav-link {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--text-secondary);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
  text-decoration: none;
}

.nav-link:hover,
.nav-link.active {
  color: var(--primary);
  background: var(--primary-ghost);
  text-decoration: none;
}

.nav-link .nav-icon {
  width: 18px;
  height: 18px;
}

/* Mobile menu toggle */
.mobile-menu-toggle {
  display: none;
  background: none;
  border: none;
  padding: var(--space-2);
  cursor: pointer;
  color: var(--text);
}

/* ---- Footer ---- */
.footer {
  background: var(--surface-sunken);
  border-top: 1px solid var(--border-subtle);
  padding: var(--space-8) 0 var(--space-5);
  margin-top: auto;
}

.footer-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-6);
  margin-bottom: var(--space-6);
}

.footer-title {
  font-size: var(--text-sm);
  font-weight: var(--font-semibold);
  color: var(--text);
  margin-bottom: var(--space-3);
}

.footer-links {
  list-style: none;
}

.footer-links li {
  margin-bottom: var(--space-2);
}

.footer-links a {
  font-size: var(--text-sm);
  color: var(--text-muted);
  transition: color var(--transition-fast);
}

.footer-links a:hover {
  color: var(--primary);
}

.footer-bottom {
  padding-top: var(--space-5);
  border-top: 1px solid var(--border-subtle);
  text-align: center;
  font-size: var(--text-sm);
  color: var(--text-muted);
}

/* ---- Loading States ---- */
.skeleton {
  background: linear-gradient(
    90deg,
    var(--surface-sunken) 25%,
    var(--border-subtle) 50%,
    var(--surface-sunken) 75%
  );
  background-size: 200% 100%;
  animation: skeleton-loading 1.5s ease-in-out infinite;
  border-radius: var(--radius-md);
}

@keyframes skeleton-loading {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

.spinner {
  width: 20px;
  height: 20px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- Progress Bar ---- */
.progress-bar {
  width: 100%;
  height: 8px;
  background: var(--surface-sunken);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.progress-bar-fill {
  height: 100%;
  background: var(--primary);
  border-radius: var(--radius-full);
  transition: width var(--transition-slow);
}

/* ---- Tooltip ---- */
.tooltip {
  position: relative;
}

.tooltip::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%) scale(0.9);
  padding: var(--space-2) var(--space-3);
  background: var(--text);
  color: var(--surface);
  font-size: var(--text-xs);
  border-radius: var(--radius-sm);
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transition: all var(--transition-fast);
  z-index: var(--z-tooltip);
}

.tooltip:hover::after {
  opacity: 1;
  transform: translateX(-50%) scale(1);
}

/* ---- Modal ---- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: oklch(0% 0 0 / 0.5);
  backdrop-filter: blur(4px);
  z-index: var(--z-modal-backdrop);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--transition-base);
}

.modal-backdrop.active {
  opacity: 1;
  pointer-events: all;
}

.modal {
  background: var(--surface-elevated);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 520px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  transform: scale(0.95) translateY(10px);
  transition: transform var(--transition-base);
}

.modal-backdrop.active .modal {
  transform: scale(1) translateY(0);
}

.modal-header {
  padding: var(--space-5) var(--space-5) 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.modal-body {
  padding: var(--space-4) var(--space-5);
  overflow-y: auto;
}

.modal-footer {
  padding: var(--space-4) var(--space-5) var(--space-5);
  display: flex;
  gap: var(--space-3);
  justify-content: flex-end;
}

/* ---- Toast Notifications ---- */
.toast-container {
  position: fixed;
  top: var(--space-4);
  right: var(--space-4);
  z-index: var(--z-toast);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  pointer-events: none;
}

.toast {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--surface-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  min-width: 300px;
  max-width: 400px;
  pointer-events: all;
  animation: toast-in 0.3s ease-out;
}

.toast.removing {
  animation: toast-out 0.3s ease-in forwards;
}

@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateX(100%);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

@keyframes toast-out {
  from {
    opacity: 1;
    transform: translateX(0);
  }
  to {
    opacity: 0;
    transform: translateX(100%);
  }
}

/* ---- Tabs ---- */
.tabs {
  display: flex;
  gap: var(--space-1);
  border-bottom: 1px solid var(--border-subtle);
  margin-bottom: var(--space-5);
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
}

.tabs::-webkit-scrollbar {
  display: none;
}

.tab {
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  color: var(--text-muted);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
  min-height: 44px;
}

.tab:hover {
  color: var(--text);
}

.tab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
}

.tab-content {
  display: none;
}

.tab-content.active {
  display: block;
  animation: fade-in 0.2s ease-out;
}

@keyframes fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ---- Copy Button ---- */
.copy-btn {
  position: relative;
}

.copy-btn.copied::after {
  content: 'Copied!';
  position: absolute;
  top: -30px;
  left: 50%;
  transform: translateX(-50%);
  padding: var(--space-1) var(--space-2);
  background: var(--text);
  color: var(--surface);
  font-size: var(--text-xs);
  border-radius: var(--radius-sm);
  white-space: nowrap;
  animation: fade-in 0.2s ease-out;
}

/* ============================================
   ANIMATIONS
   ============================================ */
@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}

.animate-fade-in-up {
  animation: fadeInUp 0.5s ease-out forwards;
}

.animate-pulse {
  animation: pulse 2s ease-in-out infinite;
}

/* Stagger children animations */
.stagger-children > * {
  opacity: 0;
  animation: fadeInUp 0.4s ease-out forwards;
}

.stagger-children > *:nth-child(1) { animation-delay: 0.05s; }
.stagger-children > *:nth-child(2) { animation-delay: 0.1s; }
.stagger-children > *:nth-child(3) { animation-delay: 0.15s; }
.stagger-children > *:nth-child(4) { animation-delay: 0.2s; }
.stagger-children > *:nth-child(5) { animation-delay: 0.25s; }
.stagger-children > *:nth-child(6) { animation-delay: 0.3s; }

/* ============================================
   DARK MODE TOGGLE
   ============================================ */
.theme-toggle {
  background: none;
  border: none;
  padding: var(--space-2);
  cursor: pointer;
  color: var(--text-secondary);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.theme-toggle:hover {
  color: var(--primary);
  background: var(--primary-ghost);
}

.theme-toggle svg {
  width: 20px;
  height: 20px;
}

/* ============================================
   UTILITY CLASSES
   ============================================ */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.text-center { text-align: center; }
.text-left { text-align: left; }
.text-right { text-align: right; }

.mt-1 { margin-top: var(--space-1); }
.mt-2 { margin-top: var(--space-2); }
.mt-3 { margin-top: var(--space-3); }
.mt-4 { margin-top: var(--space-4); }
.mt-5 { margin-top: var(--space-5); }
.mt-6 { margin-top: var(--space-6); }

.mb-1 { margin-bottom: var(--space-1); }
.mb-2 { margin-bottom: var(--space-2); }
.mb-3 { margin-bottom: var(--space-3); }
.mb-4 { margin-bottom: var(--space-4); }
.mb-5 { margin-bottom: var(--space-5); }
.mb-6 { margin-bottom: var(--space-6); }

.p-3 { padding: var(--space-3); }
.p-4 { padding: var(--space-4); }
.p-5 { padding: var(--space-5); }

.rounded { border-radius: var(--radius-md); }
.rounded-lg { border-radius: var(--radius-lg); }

.hidden { display: none !important; }

/* ============================================
   MOBILE RESPONSIVE
   ============================================ */
@media (max-width: 768px) {
  .navbar-inner {
    height: 56px;
  }
  
  .navbar-nav {
    display: none;
    position: fixed;
    top: 56px;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--surface);
    flex-direction: column;
    padding: var(--space-4);
    gap: var(--space-2);
    z-index: var(--z-sticky);
  }
  
  .navbar-nav.active {
    display: flex;
  }
  
  .nav-link {
    padding: var(--space-3) var(--space-4);
    font-size: var(--text-base);
    width: 100%;
  }
  
  .mobile-menu-toggle {
    display: block;
  }
  
  .section {
    padding: var(--space-5) 0;
  }
  
  .card {
    padding: var(--space-4);
  }
  
  .btn-lg {
    width: 100%;
  }
  
  .grid-2,
  .grid-3,
  .grid-4 {
    grid-template-columns: 1fr;
  }
  
  .modal {
    max-height: 95vh;
    margin: var(--space-3);
  }
  
  .toast-container {
    left: var(--space-4);
    right: var(--space-4);
  }
  
  .toast {
    max-width: none;
    width: 100%;
  }
  
  .footer-grid {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 480px) {
  .footer-grid {
    grid-template-columns: 1fr;
  }
  
  .tabs {
    gap: 0;
  }
  
  .tab {
    padding: var(--space-2) var(--space-3);
    font-size: var(--text-xs);
  }
}

/* ============================================
   REDUCED MOTION
   ============================================ */
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
  
  .skeleton {
    animation: none;
    background: var(--surface-sunken);
  }
}

/* ============================================
   PRINT STYLES
   ============================================ */
@media print {
  .navbar,
  .footer,
  .theme-toggle,
  .mobile-menu-toggle,
  .btn {
    display: none !important;
  }
  
  body {
    background: white;
    color: black;
  }
  
  .card {
    border: 1px solid #ccc;
    box-shadow: none;
  }
}
'''

with open(f"{output_dir}/static/css/base.css", "w") as f:
    f.write(base_css)

print("✅ base.css created")
