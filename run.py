#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Development entry point. Equivalent to ``python app.py``."""
import os

from app import app, initialize_application, logger, shutdown_application

if __name__ == '__main__':
    runtime = None
    try:
        runtime = initialize_application()
        port = int(os.environ.get('PORT', 5000))
        logger.info(f"服务启动，监听地址: http://127.0.0.1:{port}")
        app.run(host='0.0.0.0', port=port, debug=False)
    except KeyboardInterrupt:
        logger.info("接收到退出信号，服务正在关闭...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
    finally:
        shutdown_application(runtime)
