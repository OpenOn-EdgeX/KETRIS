import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'tests/browser',workers:1,use:{baseURL:process.env.KETRIS_UI_URL||'http://127.0.0.1:8080',viewport:{width:1440,height:1000},launchOptions:{...(process.env.KETRIS_CHROMIUM?{executablePath:process.env.KETRIS_CHROMIUM}:{}),args:['--no-sandbox','--disable-dev-shm-usage']}}});
