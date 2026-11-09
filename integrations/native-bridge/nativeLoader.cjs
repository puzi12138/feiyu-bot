// const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const feiyuLc = require('./qq_lifecycle.cjs').getLifecycle();

async function initializeNapCat (session, loginService, registerCallback) {
  feiyuLc.begin(session, loginService);
  feiyuLc.record(session, 'loader_init_requested');
  // const logFile = path.join(currentPath, 'napcat.log');

  console.log('[NapCat] [Info] 开始初始化NapCat');

  // fs.writeFileSync(logFile, '', { flag: 'w' });

  // fs.writeFileSync(logFile, '[NapCat] [Info] NapCat 初始化成功\n', { flag: 'a' });

  try {
    const currentPath = path.dirname(__filename);
    feiyuLc.record(session, 'loader_import_begin');
    const { NCoreInitFramework } = await import(pathToFileURL(path.join(currentPath, './napcat.mjs')).href);
    feiyuLc.record(session, 'loader_import_completed');
    feiyuLc.record(session, 'loader_framework_await_begin');
    await NCoreInitFramework(session, loginService, (callback) => { registerCallback(callback); });
    feiyuLc.record(session, 'loader_framework_await_completed');
    feiyuLc.record(session, 'loader_init_completed');
  } catch (error) {
    feiyuLc.record(session, 'loader_init_failed');
    console.log('[NapCat] [Error] Framework initialization failed; see sanitized lifecycle status.');
    // fs.writeFileSync(logFile, `[NapCat] [Error] 初始化NapCat失败: ${error.message}\n`, { flag: 'a' });
  }
}

module.exports = {
  initializeNapCat,
};
