def print_pattern():
    try:
        rows = int(input("Enter number of rows: "))
        char = input("Enter character to use: ")
        if not char:
            char = "*"
        
        for i in range(1, rows + 1):
            print(char * i)
    except ValueError:
        print("Please enter a valid integer for rows.")

if __name__ == "__main__":
    print_pattern()
