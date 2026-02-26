from langchain_core.messages import AIMessage

def router(state: State):

    last_message = state["messages"][-1]

    print("TYPE:", type(last_message))
    print("CONTENT:", getattr(last_message, "content", None))

    # tool call case
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        print("Tool call detected")
        return "tools"

    # Continue / End case
    if isinstance(last_message, AIMessage):

        content = last_message.content.strip().lower()

        if content == "continue":
            print("Continue detected")
            return "tools"

        if content == "end":
            print("End detected")
            return "End"

    print("Default End")
    return "End"
