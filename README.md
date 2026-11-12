# Novel-TTS-Toolbox
一个为日语（屎山代码）小说打造的 TTS 前端工具箱。当然，也可以用来读汉语或英语。
使用说明已经内置于 All in one toolbox.html，请下载HTML打开查看

# 运行须下载的文件
📂Novel TTS/
├── 📝 all in one toolbox.html    	# 前端工具箱
├── 📝 License.txt                	# 许可证
├── 📦 replace_rule.7z.backup     	# 开发者的规则参考包（仅供参考，不具备普适性）
├── 📝 run tts.bat                	# 启动脚本（请保存为 ANSI 编码）
├── 🐟 skill.md                   	# 提供给 AI 的技能/上下文说明文件
└── 📝 tts.py                     	# 主服务

# 首次运行后的文件目录
📂Novel TTS/
├── 📁 bg/                   	# 背景图片目录（首次运行生成）
│   ├── 🖼️ *.avif / 🖼️ *.bmp      	# 供前端设置调用的底图（需用户自行放入）
│   ├── 🖼️ *.gif  / 🖼️ *.jpeg     	# 
│   ├── 🖼️ *.jpg  / 🖼️ *.png      	# 
│   └── 🖼️ *.svg  / 🖼️ *.webp     	# 以上格式均支持（无需写后缀即可自动匹配）
│
├── 📁 model_configs/        	# 模型配置目录（首次运行生成）
│   └── 📝 default.json           	# 首次运行自动生成
│   ├── 📝 *.json                 	# 自定义配置（需用户自行建立）
│
├── 📁 replace_rules/        	# 规则目录（首次运行生成）
│   ├── 📝 auto_dict.txt          	# 自动记忆（程序自动生成，只登记，不生效）
│   ├── 📝 person_dict.txt        	# 人名词典 + [suffix] 后缀（首次运行自动生成）
│   └── 📝 user_dict.txt          	# 用户词典（首次运行自动生成，附带示例规则）
│   ├── 📝 *.txt                  	# 其他自定义规则（需用户自行建立）
│   ├── 📝 *.txt                  	# 其他解压自 replace_rule.7z 的规则文件
│
├── 📝 global_config.json         	# 全局配置（首次修改输出格式时生成）
├── 📝 model_config_active.json   	# 当前生效的模型配置文件名（运行后生成）
├── 📝 rules_enabled.json         	# 规则文件启用状态（运行后生成）
│
├── 📝 all in one toolbox.html    	# 前端工具箱
├── 📝 License.txt                	# 许可证
├── 📦 replace_rule.7z.backup     	# 开发者的规则参考包（仅供参考，不具备普适性）
├── 📝 run tts.bat                	# 启动脚本（请保存为 ANSI 编码）
├── 🐟 skill.md                   	# 提供给 AI 的技能/上下文说明文件
└── 📝 tts.py                     	# 主服务
