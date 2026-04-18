from dotenv import load_dotenv
load_dotenv()

"""
main.py — CLI entrypoint for the TUM assistant.

    python main.py setup
    python main.py crawl
    python main.py chat "ask Ksusha Zimina about HW3 task 2"
"""
import argparse, sys


def handle_message(user_message: str):
    from understand import parse_intent, confirm_intent
    from send_qa import send_qa

    print(f"[bot] Parsing: '{user_message}'")
    intent = parse_intent(user_message)

    if intent.get("action") == "clarify":
        print(f"[bot] {intent['question']}")
        answer = input("You: ").strip()
        intent = parse_intent(f"{user_message}. {answer}")

    if not confirm_intent(intent):
        print("[bot] Cancelled.")
        return

    action = intent.get("action")
    if action == "qa":
        send_qa(
            course=intent.get("course"),
            dest_type=intent.get("dest_type"),
            message=intent.get("message"),
            person=intent.get("person"),
            stream=intent.get("stream"),
            topic=intent.get("topic", "General"),
        )
    elif action == "hw":
        from submit_hw import submit_hw
        submit_hw(intent.get("course"), intent.get("sheet"), intent.get("file", ""))
    elif action == "room":
        from book_room import book_room
        book_room(intent.get("date"), intent.get("duration", 2))
    elif action == "search":
        from slide_search import search
        search(intent.get("query"))
    else:
        print(f"[bot] Unknown action: {action}")


def main():
    parser = argparse.ArgumentParser(prog="tum", description="TUM student automation assistant")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--update", action="store_true")
    p_setup.add_argument("--show",   action="store_true")
    p_setup.add_argument("--clear",  action="store_true")

    sub.add_parser("crawl")

    p_chat = sub.add_parser("chat")
    p_chat.add_argument("message")

    p_qa = sub.add_parser("qa")
    p_qa.add_argument("--course",     default=None)
    p_qa.add_argument("--type",       default=None)
    p_qa.add_argument("--message",    default=None)
    p_qa.add_argument("--person",     default=None)
    p_qa.add_argument("--stream",     default=None)
    p_qa.add_argument("--topic",      default="General")
    p_qa.add_argument("--assignment", default=None)
    p_qa.add_argument("--file",       default=None)

    p_hw = sub.add_parser("hw")
    p_hw.add_argument("--course",       required=True)
    p_hw.add_argument("--sheet",        required=True)
    p_hw.add_argument("--file",         required=True)
    p_hw.add_argument("--no-deckblatt", action="store_true")
    p_hw.add_argument("--max-size",     type=int, default=8000)

    p_room = sub.add_parser("room")
    p_room.add_argument("--date",     required=True)
    p_room.add_argument("--duration", type=int, default=2)
    p_room.add_argument("--building", default=None)

    p_idx = sub.add_parser("index")
    p_idx.add_argument("--pdf", required=True)

    p_srch = sub.add_parser("search")
    p_srch.add_argument("--query", required=True)
    p_srch.add_argument("--k", type=int, default=5)

    args = parser.parse_args()

    if args.cmd == "setup":
        from utils.credentials import register, show, clear
        if args.clear:   clear()
        elif args.show:  show()
        else:            register(update=args.update)
        return

    from utils.credentials import is_registered
    if not is_registered():
        print("\nRun: python main.py setup\n")
        sys.exit(1)

    if args.cmd == "crawl":
        from crawlers.moodle_crawler    import run as moodle_run
        from crawlers.tumonline_crawler import run as tumonline_run
        from crawlers.zulip_crawler     import run as zulip_run
        moodle_run(); tumonline_run(); zulip_run()

    elif args.cmd == "chat":
        handle_message(args.message)

    elif args.cmd == "qa":
        from send_qa import send_qa
        send_qa(args.course, args.type, args.message,
                person=args.person, stream=args.stream,
                topic=args.topic, assignment=args.assignment,
                attachment=args.file)

    elif args.cmd == "hw":
        from submit_hw import submit_hw
        submit_hw(args.course, args.sheet, args.file,
                  add_deckblatt=not args.no_deckblatt,
                  max_size_kb=args.max_size)

    elif args.cmd == "room":
        from book_room import book_room
        book_room(args.date, args.duration, building=args.building)

    elif args.cmd == "index":
        from slide_search import index_pdf
        index_pdf(args.pdf)

    elif args.cmd == "search":
        from slide_search import search
        search(args.query, top_k=args.k)


if __name__ == "__main__":
    main()
