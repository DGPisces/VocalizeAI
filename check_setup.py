#!/usr/bin/env python3
"""
Vocalize AI 项目安装检查脚本
用于验证项目是否正确设置和配置
"""

import sys
import os
from pathlib import Path

def check_python_version():
    """检查Python版本"""
    print("🔍 检查Python版本...")
    if sys.version_info < (3, 8):
        print("❌ Python版本过低，需要Python 3.8+")
        return False
    print(f"✅ Python版本: {sys.version}")
    return True

def check_dependencies():
    """检查依赖包"""
    print("\n🔍 检查依赖包...")
    required_packages = ['openai', 'sensenova', 'pygame', 'google.genai']
    missing_packages = []
    
    for package in required_packages:
        try:
            if package == 'google.genai':
                import google.genai
            else:
                __import__(package)
            print(f"✅ {package}")
        except ImportError:
            print(f"❌ {package}")
            missing_packages.append(package)
    
    if missing_packages:
        print(f"\n📦 缺少依赖包: {missing_packages}")
        print("💡 请运行: pip install -r requirements.txt")
        return False
    return True

def check_project_structure():
    """检查项目结构"""
    print("\n🔍 检查项目结构...")
    required_files = [
        'src/__init__.py',
        'src/config.py',
        'src/logger.py', 
        'src/ai_clients.py',
        'src/audio.py',
        'src/chatbot_core.py',
        'src/chatbot.py',
        'requirements.txt',
        'README.md'
    ]
    
    missing_files = []
    for file_path in required_files:
        if Path(file_path).exists():
            print(f"✅ {file_path}")
        else:
            print(f"❌ {file_path}")
            missing_files.append(file_path)
    
    if missing_files:
        print(f"\n📁 缺少文件: {missing_files}")
        return False
    return True

def check_module_imports():
    """检查模块导入"""
    print("\n🔍 检查模块导入...")
    modules = [
        'src.config',
        'src.logger',
        'src.ai_clients', 
        'src.audio',
        'src.chatbot_core',
        'src.chatbot'
    ]
    
    for module in modules:
        try:
            __import__(module)
            print(f"✅ {module}")
        except Exception as e:
            print(f"❌ {module}: {e}")
            return False
    return True

def check_configuration():
    """检查配置"""
    print("\n🔍 检查配置...")
    try:
        from src.config import get_config
        config = get_config()
        missing = config.get_missing_configs()
        
        if missing:
            print(f"⚠️  缺少环境变量: {missing}")
            print("💡 请参考README.md设置API密钥")
        else:
            print("✅ 所有配置项已设置")
        
        return True
    except Exception as e:
        print(f"❌ 配置检查失败: {e}")
        return False

def check_app_initialization():
    """检查应用初始化"""
    print("\n🔍 检查应用初始化...")
    try:
        from src.chatbot import ChatbotApp
        app = ChatbotApp()
        print("✅ 应用可以正常初始化")
        return True
    except Exception as e:
        print(f"❌ 应用初始化失败: {e}")
        return False

def main():
    """主检查函数"""
    print("🚀 Vocalize AI 项目安装检查")
    print("=" * 50)
    
    checks = [
        ("Python版本", check_python_version),
        ("依赖包", check_dependencies),
        ("项目结构", check_project_structure),
        ("模块导入", check_module_imports),
        ("配置检查", check_configuration),
        ("应用初始化", check_app_initialization)
    ]
    
    passed = 0
    total = len(checks)
    
    for name, check_func in checks:
        if check_func():
            passed += 1
    
    print("\n" + "=" * 50)
    print(f"📊 检查结果: {passed}/{total} 项通过")
    
    if passed == total:
        print("🎉 项目设置完成！可以开始使用了")
        print("\n🚀 运行命令:")
        print("   python3 -m src.chatbot")
        print("   或使用启动脚本: ./run.sh (Linux/macOS) 或 run.bat (Windows)")
    else:
        print("⚠️  还有问题需要解决，请参考上述提示")
        print("📖 详细说明请查看 README.md")
    
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 