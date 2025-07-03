# Vocalize AI Chatbot

这是一个基于AI的餐厅预订聊天机器人项目，旨在通过自然语言处理技术简化用户预订餐厅的流程。

## 项目结构

- `src/`：包含项目的核心源代码，例如 `chatbot.py` (主逻辑) 和 `api.py` (API 密钥配置)。
- `logs/`：存放运行日志和AI自我反思日志。
- `requirements.txt`：列出项目所需的所有Python依赖。
- `.gitignore`：Git 版本控制忽略文件配置。

## 安装与运行

1. **克隆仓库**：
   ```bash
   git clone <仓库地址>
   cd Vocalize AI
   ```

2. **安装依赖**：
   推荐使用 `pip` 安装项目依赖：
   ```bash
   pip install -r requirements.txt
   ```

3. **配置 API 密钥**：
   本项目使用环境变量加载 API 密钥。请在运行程序前，设置以下环境变量：
   ```bash
   export OPENAI_API_KEY="你的OpenAI API密钥"
   export SENSENOVA_ACCESS_KEY_ID="你的Sensenova Access Key ID"
   export SENSENOVA_SECRET_ACCESS_KEY="你的Sensenova Secret Access Key"
   ```
   (请注意：在生产环境中，建议使用更安全的方式管理环境变量，例如使用 .env 文件并将其加入 .gitignore)

4. **运行程序**：
   ```bash
   python src/chatbot.py
   ```

## 主要功能

- **智能问答与信息补全**：AI 助手能够根据用户预订需求进行多轮对话，智能判断缺失信息（如联系方式、时间、人数等），并主动向用户追问以完成预订。
- **专业商家沟通**：以简洁、直接、专业的口吻，将用户的完整预订需求和最新决策准确转述给商家。
- **清晰用户反馈**：根据商家回复，用自然、友好、专业的语言向用户转述商家最新回复或最终预订结果，并引导用户进行下一步决策。
- **智能对话管理**：能够分类商家回复类型（如"等待处理"、"预订成功"、"需要用户补充信息"），并根据不同类型智能驱动对话流程。
- **AI 自我反思与改进**：具备AI自我反思机制，记录对话过程中的表现，指出存在的问题，并精炼改进建议，以持续提升对话质量。
- **完整日志记录**：自动记录用户、AI 和商家之间的完整对话日志，便于复盘和分析。

## 贡献

欢迎贡献！如果你有任何改进建议或发现bug，请提交 Pull Request 或 Issue。

## 许可证

本项目采用 MIT 许可证，详情请参阅 `LICENSE` 文件。

---

# Vocalize AI Chatbot

This is an AI-powered restaurant reservation chatbot project, aiming to simplify the user's restaurant booking process through natural language processing technology.

## Project Structure

- `src/`: Contains the core source code of the project, such as `chatbot.py` (main logic) and `api.py` (API key configuration).
- `logs/`: Stores runtime logs and AI self-reflection logs.
- `requirements.txt`: Lists all Python dependencies required by the project.
- `.gitignore`: Git version control ignore file configuration.

## Installation and Running

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/DGPisces/Vocalize-AI
    cd Vocalize AI
    ```

2.  **Install Dependencies**:
    It is recommended to install project dependencies using `pip`:
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure API Keys**:
    This project uses environment variables to load API keys. Please set the following environment variables before running the program:
    ```bash
    export OPENAI_API_KEY="Your OpenAI API Key"
    export SENSENOVA_ACCESS_KEY_ID="Your Sensonova Access Key ID"
    export SENSENOVA_SECRET_ACCESS_KEY="Your Sensonova Secret Access Key"
    ```
    (Note: In a production environment, it is recommended to manage environment variables more securely, for example, by using a .env file and adding it to .gitignore)

4.  **Run the Program**:
    ```bash
    python src/chatbot.py
    ```

## Key Features

-   **Intelligent Questioning & Information Completion**: The AI assistant can conduct multi-turn conversations based on user reservation requests, intelligently identify missing information (such as contact details, time, number of people, etc.), and proactively ask the user for it to complete the reservation.
-   **Professional Merchant Communication**: Accurately relays the user's complete reservation needs and latest decisions to the merchant in a concise, direct, and professional tone.
-   **Clear User Feedback**: Based on the merchant's reply, it naturally, friendly, and professionally conveys the merchant's latest response or final reservation result to the user, and guides the user to the next step.
-   **Smart Dialogue Management**: Capable of classifying merchant reply types (e.g., "waiting for processing", "reservation successful", "user needs to provide more info"), and intelligently driving the dialogue process based on different types.
-   **AI Self-Reflection & Improvement**: Equipped with an AI self-reflection mechanism, it records its performance during the conversation, identifies problems, and refines improvement suggestions to continuously enhance dialogue quality.
-   **Comprehensive Log Recording**: Automatically records complete dialogue logs between users, AI, and merchants for review and analysis.

## Contribution

Contributions are welcome! If you have any suggestions for improvement or find bugs, please submit a Pull Request or Issue.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details. 