def get_input():
    """Prompts the user for input and returns the value."""
    try:
        return input("Enter value for x: ")
    except EOFError:
        return None

def process_value(x):
    """Processes the input value and returns a result."""
    try:
        val = float(x)
        return f"Processed numeric value: {val * 2}"
    except (ValueError, TypeError):
        return f"Processed string value: {str(x).upper()}"

def main():
    """Main execution flow."""
    x = get_input()
    if x is not None:
        result = process_value(x)
        print(f"Input x is: {x}")
        print(result)
    else:
        print("No input provided.")

if __name__ == "__main__":
    main()
