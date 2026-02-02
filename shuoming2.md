                                                                                                        
● 已创建新版本启动脚本 start_v2.sh。                                                                    
                                                                                                        
  主要变更                                                                                              
  ┌──────┬────────────────┬──────────────────┐                                                          
  │ 项目 │ 旧版 start.sh  │ 新版 start_v2.sh │                                                          
  ├──────┼────────────────┼──────────────────┤                                                          
  │ 入口 │ api_server:app │ app.main:app     │                                                          
  ├──────┼────────────────┼──────────────────┤                                                          
  │ 版本 │ 未标注         │ v2.0.0           │                                                          
  ├──────┼────────────────┼──────────────────┤                                                          
  │ 架构 │ 单文件         │ 模块化           │                                                          
  └──────┴────────────────┴──────────────────┘                                                          
  使用方法                                                                                              
                                                                                                        
  # 启动服务                                                                                            
  bash /www/wwwroot/ai-history-manager/start_v2.sh                                                      
                                                                                                        
  # 其他命令                                                                                            
  bash /www/wwwroot/ai-history-manager/start_v2.sh stop      # 停止                                     
  bash /www/wwwroot/ai-history-manager/start_v2.sh restart   # 重启                                     
  bash /www/wwwroot/ai-history-manager/start_v2.sh status    # 状态                                     
  bash /www/wwwroot/ai-history-manager/start_v2.sh logs      # 日志                                     
                                                                           