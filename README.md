# Vocalize AI Chatbot

这是一个基于AI的餐厅预订聊天机器人项目，采用现代化的模块化架构，支持语音交互和AI自我反思功能。

## 核心功能

### 智能餐厅预订助手
- 通过自然语言对话帮助用户预订餐厅
- 自动收集预订所需信息（时间、人数、联系方式等）
- 智能判断信息完整性，主动追问缺失信息

### 多方对话协调
- **用户 ↔ AI ↔ 商家** 三方对话桥梁
- AI以用户身份向商家转述预订需求
- AI向用户转述商家回复并引导下一步操作

### 语音交互功能
- 使用Google AI Gemini 2.5 Flash Preview TTS引擎生成语音
- 支持实时语音播放，提升用户体验
- 智能语音反馈系统

### AI自我反思与改进
- 记录完整对话过程并自动分析表现
- 识别对话中的问题并生成改进建议
- 智能精炼反思日志，持续优化对话质量

## 项目结构

```
Vocalize AI/
├── src/                     # 核心源代码目录
│   ├── __init__.py         # 包初始化文件
│   ├── config.py           # 配置管理模块 
│   ├── logger.py           # 统一日志管理 
│   ├── ai_clients.py       # AI客户端管理 
│   ├── audio.py            # 音频处理模块 
│   ├── chatbot_core.py     # 核心业务逻辑 
│   ├── chatbot.py          # 主程序入口 
│   └── api.py              # 兼容性保留 
├── logs/                   # 日志文件目录
├── requirements.txt        # Python依赖列表
├── pyproject.toml         # 项目配置文件 
├── run.sh                 # Linux/macOS启动脚本
├── run.bat                # Windows启动脚本
└── README.md              # 项目文档



## 安装与运行

### 1. 克隆仓库
```bash
git clone https://github.com/DGPisces/VocalizeAI
cd "Vocalize AI"
```

### 2. 安装依赖
```bash
pip install -r requirements.txt
```

**核心依赖包**：
- `openai` - OpenAI/DeepSeek API客户端
- `sensenova` - SenseNova API支持
- `pygame` - 音频播放功能
- `google-genai` - Google AI语音生成

### 3. 配置 API 密钥

项目支持多种配置方式，请选择适合您的方法：

#### 方法一：环境变量配置 (推荐)

**Linux / macOS**：
```bash
export OPENAI_API_KEY="你的OpenAI API密钥"
export SENSENOVA_ACCESS_KEY_ID="你的Sensenova Access Key ID"  
export SENSENOVA_SECRET_ACCESS_KEY="你的Sensenova Secret Access Key"
export GOOGLE_API_KEY="你的Google API密钥"
export OPENAI_BASE_URL="https://api.sensenova.cn/compatible-mode/v1/"
export OPENAI_MODEL="DeepSeek-V3"
export GOOGLE_MODEL_ID="gemini-2.5-flash-preview-tts"
```

**Windows**：
```cmd
set OPENAI_API_KEY=你的OpenAI API密钥
set SENSENOVA_ACCESS_KEY_ID=你的Sensenova Access Key ID
set SENSENOVA_SECRET_ACCESS_KEY=你的Sensenova Secret Access Key  
set GOOGLE_API_KEY=你的Google API密钥
set OPENAI_BASE_URL=https://api.sensenova.cn/compatible-mode/v1/
set OPENAI_MODEL=DeepSeek-V3
set GOOGLE_MODEL_ID=gemini-2.5-flash-preview-tts
```

#### 方法二：使用启动脚本

编辑对应的启动脚本，填入您的API密钥：
- **Linux/macOS**: 编辑 `run.sh`
- **Windows**: 编辑 `run.bat`

### 4. 运行程序

#### 方法一：直接运行 (推荐)
```bash
# Linux/macOS
python3 -m src.chatbot

# Windows  
python3 -m src.chatbot
```

#### 方法二：使用启动脚本
```bash
# Linux/macOS
./run.sh

# Windows
run.bat
```

## 💡 使用示例

```
用户：我想预订明晚7点的位子，4个人
AI：请提供您的联系方式以便确认预订
用户：我的电话是138xxxxxxxx  
AI→商家：我需要预订明天晚上7点的位子，4人用餐，联系电话138xxxxxxxx
商家：7点已满，6点或8点可以吗？
AI→用户：商家表示7点已满，可以选择6点或8点，您希望哪个时间？
用户：6点可以
AI→商家：我同意6点的时间，请确认预订
商家：好的，已为您预订成功  
AI→用户：预订成功！为您安排了明天晚上6点的位子...
```

## 🏗️ 技术架构

### 模块化设计
- **配置管理** (`config.py`) - 统一的环境变量和配置管理
- **日志系统** (`logger.py`) - 对话日志和反思日志管理
- **AI客户端** (`ai_clients.py`) - OpenAI和Google AI客户端管理
- **音频处理** (`audio.py`) - 语音生成和播放功能
- **核心逻辑** (`chatbot_core.py`) - 餐厅预订业务逻辑
- **主程序** (`chatbot.py`) - 应用程序入口和流程控制

## 🔍 故障排除

### 常见问题

1. **缺少环境变量**
   ```bash
   WARNING - 缺少以下环境变量: GOOGLE_API_KEY
   ```
   **解决方案**: 按照配置部分设置所需的API密钥

2. **模块导入错误**
   ```bash
   ModuleNotFoundError: No module named 'src'
   ```
   **解决方案**: 确保在项目根目录运行，使用 `python3 -m src.chatbot`

3. **Python命令不存在**
   ```bash
   command not found: python
   ```
   **解决方案**: 使用 `python3` 代替 `python`

### 🔧 安装验证

我们提供了一个便捷的检查脚本来验证项目是否正确设置：

```bash
python3 check_setup.py
```

这个脚本会检查：
- Python版本兼容性
- 依赖包安装状态
- 项目文件完整性
- 模块导入功能
- 配置验证
- 应用初始化

### 手动测试
```bash
# 测试模块导入
python3 -c "import src.config; print('配置模块正常')"

# 测试配置验证
python3 -c "from src.config import get_config; print('缺失配置:', get_config().get_missing_configs())"

# 测试程序初始化
python3 -c "from src.chatbot import ChatbotApp; app = ChatbotApp(); print('程序初始化成功')"
```

## 许可证

本项目采用 MIT 许可证。详情请参阅 [LICENSE](LICENSE) 文件。

---

<div align="center">

**🌟 如果这个项目对您有帮助，请考虑给我们一个星标！**

</div> 