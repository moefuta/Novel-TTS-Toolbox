# 写在前面
本工具在本质上是一个用于 **生成词典** 的工具，算法中绝大多数是为了生成词典服务的，真正用于TTS中转的部分其实很小。

待词典足够丰富之后，会单独出一个纯后台静默运行的版本。

# Novel-TTS-Toolbox
一个为日语（屎山代码）小说打造的 TTS 前端工具箱。当然，也可以用来读汉语或英语。
使用说明已经内置于 All in one toolbox.html，请下载HTML打开查看

日文里有非常多的小字，比如说｜凝視《ぎょうし》，但是epub的ruby标注通常是这样的<ruby>凝<rt>ぎよう</rt>視<rt>し</rt></ruby>。亦即，因为注音本身已经很小了，他不能再进一步缩小否则看不清了，所以Ruby注音便不再区分大字小字。

于是乎，把ぎよう修复成ぎょう，就是这个工具的主要工作。只不过距离真正有用，还需要我花点时间多跑一些样本，形成词典。

<img width="779" height="568" alt="sample 1" src="https://github.com/user-attachments/assets/878bcad2-792a-46a5-9348-729cf55cc694" />


# 运行须下载的文件
```
📂Novel TTS/                	# 文件目录请自行建立
├── 📝 all in one toolbox.html    	# 前端工具箱
├── 📝 License.txt                	# 许可证
├── 📦 replace_rule.7z.backup     	# 开发者的规则参考包（仅供参考，不具备普适性）
├── 📝 run tts.bat                	# 启动脚本（请保存为 ANSI 编码）
├── 🐟 skill.md                   	# 提供给 AI 的技能/上下文说明文件
└── 📝 tts.py                     	# 主服务
```

# 运行需要进行的设置
1. 在小说阅读器中，选择Open AI接口
2. API端口为`http://127.0.0.1:8000`
3. API key随便写一个`123456`
4. Model ID填写`auto`
5. 开始运行

# 目前已经支持的TTS引擎
1. 本地软件 CeVIO AI.exe 但是COM调用 - 可以配置详细的情感参数 - 推荐
2. 本地软件 CeVIO AI.exe 但是SAPI接口 - 仍然有机械感
3. 本地软件 VOICEVOX.exe - 机械感
4. 联网服务 EdgeTTS - 机械感
5. 本地Windows讲述人 SAPI接口 - 极其机械感

# 首次运行后的文件目录
```
📂Novel TTS/                	# 文件目录请自行建立
├── 📁 bg/                   	# 背景图片目录（首次运行生成）
│   ├── 🖼️ *.avif / 🖼️ *.bmp      	# 供前端设置调用的底图（需用户自行放入）
│   ├── 🖼️ *.gif  / 🖼️ *.jpeg     	# 
│   ├── 🖼️ *.jpg  / 🖼️ *.png      	# 
│   └── 🖼️ *.svg  / 🖼️ *.webp     	# 以上格式均支持（无需写后缀即可自动匹配）
│
├── 📁 model_configs/        	# 模型配置目录（首次运行生成）
│   ├── 📝 default.json           	# 首次运行自动生成
│   └── 📝 *.json                 	# 自定义配置（需用户自行建立）
│
├── 📁 replace_rules/        	# 规则目录（首次运行生成）
│   ├── 📝 auto_dict.txt          	# 自动记忆（程序自动生成，只登记，不生效）
│   ├── 📝 person_dict.txt        	# 人名词典 + [suffix] 后缀（首次运行自动生成）
│   ├── 📝 user_dict.txt          	# 用户词典（首次运行自动生成，附带示例规则）
│   ├── 📝 *.txt                  	# 其他自定义规则（需用户自行建立）
│   └── 📝 *.txt                  	# 其他解压自 replace_rule.7z 的规则文件
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
```

# 工作流程建议图解
有两种主要的流程：Route A 边朗读边攒词典，Route B 先做词典再朗读。两者可以混用，最后都会汇入 replace_rules/。

## Route A
```
打开阅读器 → 选 LocalTTS → Model ID 填 auto → 开始朗读
      ↓
阅读器把文本发到 http://127.0.0.1:8000
      ↓
tts.py 收到文本，按 replace_rules/ 里的词典处理
      ↓
词典里没有的「原文 + 读音」，顺手登记到 auto_dict.txt
      ↓
（auto_dict.txt 只是登记本，不会直接参与后续替换）
      ↓
把 auto_dict.txt 连同根目录的 skill.md 一起丢给 AI
      ↓
AI 帮你校验、修复 ruby 注音（比如把 ぎよう 修成 ぎょう）
      ↓
用户手动把修好的词条复制进 replace_rules/ 下的其他 *.txt
      ↓
这些 *.txt 从此参与 tts.py 的词典匹配
```

## Route B
```
开始阅读前，先把 EPUB 丢进工具箱提取
      ↓
导出 user_dict_书名.txt（文件名自动带上书名）
      ↓
连同根目录的 skill.md 一起丢给 AI
      ↓
AI 帮你校验、修复 ruby 注音
      ↓
把修好的 user_dict_书名.txt 放进 replace_rules/
      ↓
打开阅读器开始朗读（词典已就位）
```

需要注意，Route B使用的是HTML算法，其提取效果是远不如Route A的python算法的，建议两者结合使用。
