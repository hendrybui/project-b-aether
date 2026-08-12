import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    watch: {
      ignored: [
        // The repo root mixes in several huge trees (venvs, sibling
        // experiments, backups) that together exceed the inotify watch limit
        // (fs.inotify.max_user_watches) and crash the dev server with ENOSPC
        // if vite tries to watch them all. Keep the watcher on src/ only.
        '**/audiomass/**',
        '**/.venv/**',
        '**/venv*/**',
        '**/splinter-x/**',
        '**/dj_toolkit/**',
        '**/music-tools/**',
        '**/PROJECT_X-Splinter/**',
        '**/Project-B/**',
        '**/backups/**',
        '**/dist/**',
        '**/exports/**',
        '**/samples/**',
        '**/node_modules/.vite/**',
      ],
    },
  },
})
