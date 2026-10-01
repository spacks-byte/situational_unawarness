import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import test functions from other modules
from utilities import (
    test_check_server_time,
    test_get_exchange_info,
    test_get_pending_count,
    test_get_ticker,
)
from balance import test_get_balance
from trades import test_place_order, test_query_order, test_cancel_order
from portfolio_worth import test_get_portfolio_worth
from purchase_by_value import test_buy_coin_by_value
from shorts import test_close_short, test_get_short_positions, test_open_short


def display_menu():
    """Display the main menu options."""
    print("\n" + "="*50)
    print("    ROOSTOO API TESTING MENU")
    print("="*50)
    print("1. Check Server Time (No Auth)")
    print("2. Get Exchange Info (No Auth)")
    print("3. Get Ticker (All Pairs)")
    print("4. Get Ticker (Specific Coin)")
    print("5. Get Account Balance")
    print("6. Place Order (Test Menu)")
    print("7. Query Orders")
    print("8. Cancel Orders")
    print("9. Get Portfolio Worth (USD)")
    print("10. Buy Coin By USD Value (MARKET)")
    print("11. Get Pending Order Count")
    print("12. Get Open Short Positions")
    print("13. Open Short Position")
    print("14. Close Short Position")
    print("0. Exit")
    print("="*50)


def get_user_input(prompt, input_type=str):
    """Get user input with type validation."""
    while True:
        try:
            user_input = input(prompt)
            if input_type == int:
                return int(user_input)
            elif input_type == float:
                return float(user_input)
            else:
                return user_input
        except ValueError:
            print(f"Invalid input. Please enter a valid {input_type.__name__}.")


def handle_place_order_menu():
    """Handle the place order submenu."""
    print("\n--- Place Order Test Menu ---")
    print("0. Custom order")
    print("1. LIMIT order (BNB SELL 0.1 at 965)")
    print("2. MARKET order (BNB/USD BUY 0.1)")
    print("3. Invalid order test (LIMIT without price)")
    print("4. Quit")

    test_num = get_user_input("Choose test (0-4): ", int)
    if test_num in [0, 1, 2, 3]:
        test_place_order(test_num)
    elif test_num == 4:
        return
    else:
        print("Invalid test number. Please choose 0-4.")


def handle_ticker_with_coin():
    """Handle ticker request for specific coin."""
    coin = get_user_input("Enter coin symbol (e.g., BTC): ").upper()
    test_get_ticker(coin)


def handle_query_order_with_coin():
    """Handle query order for specific coin."""
    coin = get_user_input("Enter trading pair (e.g., BTC/USD): ").upper()
    test_query_order(coin)


def handle_cancel_order_with_coin():
    """Handle cancel order for specific coin."""
    coin = get_user_input("Enter trading pair (e.g., BTC/USD): ").upper()
    test_cancel_order(coin)


def check_environment():
    """Check if required environment variables are set."""
    required_vars = ['ROOSTOO_API_KEY', 'ROOSTOO_API_SECRET', 'BASE_URL']
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print("\n[WARNING] Missing environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease check your .env file and ensure all variables are set.")
        print("Some functions may not work without proper credentials.\n")
        return False
    else:
        print("[OK] All environment variables are set.")
        return True


def main():
    """Main function to run the menu system."""
    print("Welcome to Roostoo API Testing Suite!")
    
    # Check environment variables
    env_ok = check_environment()
    
    while True:
        display_menu()
        
        try:
            choice = get_user_input("Enter your choice (0-14): ", int)
            
            if choice == 0:
                print("\nExiting... Goodbye!")
                break
            elif choice == 1:
                print("\nChecking server time...")
                test_check_server_time()
            elif choice == 2:
                print("\n[DATA] Getting exchange info...")
                test_get_exchange_info()
            elif choice == 3:
                print("\n[DATA] Getting ticker for all pairs...")
                test_get_ticker()
            elif choice == 4:
                print("\n[DATA] Getting ticker for specific coin...")
                handle_ticker_with_coin()
            elif choice == 5:
                if not env_ok:
                    print("\n[ERROR] Cannot get balance without proper API credentials.")
                    continue
                print("\n[ACCOUNT] Getting account balance...")
                test_get_balance()
            elif choice == 6:
                if not env_ok:
                    print("\n[ERROR] Cannot place orders without proper API credentials.")
                    continue
                handle_place_order_menu()
            elif choice == 7:
                if not env_ok:
                    print("\n[ERROR] Cannot query orders without proper API credentials.")
                    continue
                print("\n[ORDERS] Querying all orders...")
                handle_query_order_with_coin()
            elif choice == 8:
                if not env_ok:
                    print("\n[ERROR] Cannot cancel orders without proper API credentials.")
                    continue
                print("\n[ERROR] Canceling all orders...")
                handle_cancel_order_with_coin()
            elif choice == 9:
                if not env_ok:
                    print("\n[ERROR] Cannot compute portfolio worth without proper API credentials.")
                    continue
                print("\n[ACCOUNT] Computing current portfolio worth...")
                test_get_portfolio_worth()
            elif choice == 10:
                if not env_ok:
                    print("\n[ERROR] Cannot place orders without proper API credentials.")
                    continue
                print("\n[ORDERS] Buying coin by USD value...")
                test_buy_coin_by_value()
            elif choice == 11:
                if not env_ok:
                    print("\n[ERROR] Cannot get pending order count without proper API credentials.")
                    continue
                print("\n[ORDERS] Getting pending order count...")
                test_get_pending_count()
            elif choice == 12:
                if not env_ok:
                    print("\n[ERROR] Cannot get short positions without proper API credentials.")
                    continue
                print("\n[POSITIONS] Getting open short positions...")
                test_get_short_positions()
            elif choice == 13:
                if not env_ok:
                    print("\n[ERROR] Cannot open a short without proper API credentials.")
                    continue
                print("\n[POSITIONS] Opening short position...")
                test_open_short()
            elif choice == 14:
                if not env_ok:
                    print("\n[ERROR] Cannot close a short without proper API credentials.")
                    continue
                print("\n[POSITIONS] Closing short position...")
                test_close_short()
            else:
                print("\n[ERROR] Invalid choice. Please enter a number between 0-14.")
                
        except KeyboardInterrupt:
            print("\n\nOperation cancelled by user. Exiting...")
            break
        except Exception as e:
            print(f"\n[ERROR] An error occurred: {e}")
            print("Please try again.")
        
        # Pause before showing menu again
        input("\nPress Enter to continue...")


if __name__ == "__main__":
    main()
