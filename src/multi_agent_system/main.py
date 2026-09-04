import uuid
import os
import sys
from pprint import pprint

from .config import get_settings
from .graph import build_graph
from .state import create_initial_state
from .storage import RedisStateStore


def run() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("当前项目需要 Python 3.10+，请切换解释器后重试。")

    settings = get_settings()
    if not settings.openai_api_key:
        raise ValueError("请先在 .env 中设置 OPENAI_API_KEY")

    app = build_graph(settings)
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    task = input("请输入任务描述: ").strip()
    initial_state = create_initial_state(task, thread_id)

    # final_state = app.invoke(initial_state, config=config)
    print("\n🚀 正在启动 Multi-Agent 工作流...")
    # app.stream 会在每个节点执行完毕后产出该节点的状态更新。
    for output in app.stream(initial_state, config=config):
        # output 是一个字典，键是当前执行的节点名称，值是更新后的状态
        for node_name, state_update in output.items():
            print(f"\n==============================================")
            print(f"🤖 当前节点: [{node_name.upper()}] 执行完毕")
            print(f"==============================================")
            
            # 提取该节点生成的最新消息并打印
            if "messages" in state_update and state_update["messages"]:
                last_message = state_update["messages"][-1]
                print(last_message.content)
            
    # 并行 Worker 的 reducer 合并结果以 checkpoint 中的最终状态为准。
    final_state = dict(app.get_state(config).values)

    # 提取状态中记录的所有代码片段
    code_snippets = final_state.get("code_snippets", [])
    if code_snippets:
        # 在 src 目录下创建一个专属的工作区文件夹存放代码
        workspace_dir = os.path.join(os.path.dirname(__file__), "workspace")
        os.makedirs(workspace_dir, exist_ok=True)
        
        print(f"\n📁 发现 {len(code_snippets)} 段由 Agent 生成的代码，正在保存...")
        
        for index, code in enumerate(code_snippets):
            # 将生成的代码保存为 .py 文件
            file_path = os.path.join(workspace_dir, f"agent_generated_code_{index + 1}.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
            print(f"  👉 已保存文件: {file_path}")

    print("\n=== Final State ===")
    pprint(final_state)

    if settings.use_redis:
        store = RedisStateStore(settings.redis_url)
        store.save_state(thread_id, final_state)
        print(f"\nRedis 已保存状态: thread_id={thread_id}")


if __name__ == "__main__":
    run()
