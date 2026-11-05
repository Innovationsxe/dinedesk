import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import cartBrandMark from '../images/dinedesk-cart-mark.png';
import './public-app.css';

const publicApi = window.dineDeskPublic;
const fallbackImage = 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=1400&q=85';

function BrandMark() {
  return <svg className="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 17h16M6 17a6 6 0 0 1 12 0M12 5v1M4 20h16" />
  </svg>;
}

function useMenuItems() {
  const [items, setItems] = useState(() => publicApi.getMenuItems());
  useEffect(() => {
    const update = (event) => setItems(event.detail);
    window.addEventListener('dinedesk:menu-updated', update);
    return () => window.removeEventListener('dinedesk:menu-updated', update);
  }, []);
  return items;
}

function useCartItems() {
  const [items, setItems] = useState(() => publicApi.getCart());
  useEffect(() => {
    const update = (event) => setItems(event.detail.map((item) => ({ ...item })));
    window.addEventListener('dinedesk:cart-updated', update);
    return () => window.removeEventListener('dinedesk:cart-updated', update);
  }, []);
  return items;
}

function useReveal() {
  useEffect(() => {
    const elements = document.querySelectorAll('[data-reveal]:not(.is-visible)');
    if (!('IntersectionObserver' in window)) {
      elements.forEach((element) => element.classList.add('is-visible'));
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.12 });
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  });
}

function Reveal({ children, className = '', delay = 0 }) {
  return <div className={`reveal-up ${className}`} data-reveal style={{ '--reveal-delay': `${delay}ms` }}>{children}</div>;
}

function getCategories(items) {
  return ['All', ...new Set(items.map((item) => item.category).filter(Boolean))];
}

function FoodCard({ item, index = 0, compact = false, onImageClick }) {
  const image = publicApi.imageUrl(item);
  const price = Number(item.currentPrice ?? item.price);
  return (
    <Reveal className={`food-card${compact ? ' food-card-compact' : ''}`} delay={index * 70}>
      <div className="food-card-image">
        {onImageClick
          ? <button type="button" className="food-card-image-trigger" onClick={(event) => onImageClick(item, event.currentTarget.querySelector('img')?.currentSrc || image)} aria-label={`View a larger photo of ${item.name}`}>
              <img src={image} alt="" loading={index < 2 ? 'eager' : 'lazy'} onError={(event) => { event.currentTarget.src = fallbackImage; }} />
            </button>
          : <img src={image} alt={item.name} loading={index < 2 ? 'eager' : 'lazy'} onError={(event) => { event.currentTarget.src = fallbackImage; }} />}
        {item.discountPercent > 0 && <span className="food-card-tag">{item.discountPercent}% off</span>}
        {compact && <button type="button" className="food-card-add" onClick={() => publicApi.addToCart(item.name)} aria-label={`Add ${item.name} to your order`}>+</button>}
      </div>
      <div className="food-card-copy">
        <div className="food-card-kicker"><span>{item.category || 'From the kitchen'}</span><span className="food-card-price">R{price.toFixed(2)}</span></div>
        <h3>{item.name}</h3>
        <p>{item.description || 'Thoughtfully prepared with the finest ingredients.'}</p>
      </div>
    </Reveal>
  );
}

function FoodImageDialog({ selection, onClose }) {
  const dialogRef = useRef(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (selection && !dialog.open) dialog.showModal();
    else if (!selection && dialog.open) dialog.close();
  }, [selection]);

  return (
    <dialog
      ref={dialogRef}
      className="food-image-dialog"
      aria-label={selection ? `Photo of ${selection.item.name}` : undefined}
      onClose={onClose}
      onClick={(event) => { if (event.target === event.currentTarget) event.currentTarget.close(); }}
    >
      {selection && <>
        <button type="button" className="food-image-dialog-close" onClick={() => dialogRef.current.close()} aria-label="Close food photo">×</button>
        <img src={selection.image} alt={selection.item.name} />
        <p>{selection.item.name}</p>
      </>}
    </dialog>
  );
}

function CartButton({ count }) {
  return <button type="button" className="lux-cart-button" onClick={() => publicApi.showPage('orders')} aria-label={`Open your basket, ${count} items selected`}>
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h2l2.2 10.1a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 1.9-1.4L21 8H6" /><circle cx="10" cy="20" r="1" /><circle cx="18" cy="20" r="1" /></svg>
    <span>View basket</span><b>{count}</b>
  </button>;
}

function RestaurantFooter({ kitchen = false }) {
  const browseMenu = () => {
    if (kitchen) {
      publicApi.returnHome();
      window.setTimeout(() => publicApi.scrollToSection('explore-menu'), 100);
      return;
    }
    publicApi.scrollToSection('explore-menu');
  };
  const browseKitchen = () => {
    if (kitchen) {
      publicApi.returnHome();
      window.setTimeout(() => publicApi.scrollToSection('kitchen-story'), 100);
      return;
    }
    publicApi.scrollToSection('kitchen-story');
  };
  const backToTop = () => window.scrollTo({ top: 0, behavior: 'smooth' });

  return (
    <footer className="restaurant-footer">
      <div className="restaurant-footer-grid">
        <div className="restaurant-footer-brand">
          <h2><BrandMark />DineDesk</h2>
          <p>DineDesk brings the menu, ordering and thoughtful restaurant service together in one easy experience.</p>
          <span>Thoughtful dining, made easy.</span>
        </div>
        <div className="restaurant-footer-column">
          <h3>Explore</h3>
          <button type="button" onClick={browseMenu}>Browse the menu</button>
          <button type="button" onClick={browseKitchen}>Our kitchen</button>
        </div>
        <div className="restaurant-footer-column">
          <h3>Guest service</h3>
          <button type="button" onClick={() => publicApi.showPage('orders')}>Start an order</button>
          <button type="button" onClick={() => publicApi.showPage('customer-orders')}>View past orders</button>
          <p>Choose dine-in or takeaway when placing your order.</p>
        </div>
        <div className="restaurant-footer-column">
          <h3>At your table</h3>
          <p>Our team is here to make every part of your meal feel easy, from choosing a favourite to enjoying the final bite.</p>
          <button type="button" onClick={backToTop}>Back to the top</button>
        </div>
      </div>
      <div className="restaurant-footer-bottom">
        <span>© {new Date().getFullYear()} DineDesk. All rights reserved.</span>
        <span>Made for memorable meals.</span>
      </div>
    </footer>
  );
}

function HomePage() {
  const menuItems = useMenuItems();
  const cartItems = useCartItems();
  const [category, setCategory] = useState('All');
  const [selectedFoodImage, setSelectedFoodImage] = useState(null);
  const [activeSection, setActiveSection] = useState('menu');
  const [underline, setUnderline] = useState({ left: 0, width: 0 });
  const menuLink = useRef(null);
  const kitchenLink = useRef(null);
  const navLinks = useRef(null);
  useReveal();
  const categories = getCategories(menuItems);
  const visibleItems = category === 'All' ? menuItems : menuItems.filter((item) => item.category === category);
  const count = cartItems.reduce((total, item) => total + item.quantity, 0);

  useEffect(() => {
    const link = activeSection === 'menu' ? menuLink.current : kitchenLink.current;
    const links = navLinks.current;
    if (!link || !links) return undefined;
    const updateUnderline = () => setUnderline({ left: link.offsetLeft, width: link.offsetWidth });
    updateUnderline();
    window.addEventListener('resize', updateUnderline);
    return () => window.removeEventListener('resize', updateUnderline);
  }, [activeSection]);

  const goToSection = (section) => {
    setActiveSection(section === 'explore-menu' ? 'menu' : 'kitchen');
    publicApi.scrollToSection(section);
  };

  return (
    <div className="lux-site home-site">
      <nav className="lux-nav">
        <button type="button" className="lux-wordmark" onClick={() => publicApi.showPage('login')} aria-label="DineDesk staff login">
          <BrandMark />
          <span>DineDesk</span>
        </button>
        <div className="lux-nav-links" ref={navLinks}>
          <button ref={menuLink} type="button" className="lux-nav-tab" onClick={() => goToSection('explore-menu')}>The menu</button>
          <button ref={kitchenLink} type="button" className="lux-nav-tab" onClick={() => goToSection('kitchen-story')}>Our kitchen</button>
          <button type="button" className="lux-order-history-link" onClick={() => publicApi.showPage('customer-orders')} aria-label="View all my orders">My orders</button>
          <CartButton count={count} />
          <span className="lux-nav-indicator" style={{ transform: `translateX(${underline.left}px)`, width: `${underline.width}px` }} />
        </div>
      </nav>

      <main>
        <section className="lux-hero">
          <div className="lux-hero-image" role="img" aria-label="A South African braai platter prepared for sharing">
            <img src="https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=2400&q=90" alt="" fetchPriority="high" />
          </div>
          <div className="lux-hero-shade" />
          <div className="lux-hero-content">
            <p className="lux-eyebrow lux-hero-reveal"><span /> A little more occasion, every day</p>
            <h1 className="lux-hero-title lux-hero-reveal">A table worth<br /><em>remembering.</em></h1>
            <p className="lux-hero-description lux-hero-reveal">An elegant pause in the everyday. Thoughtful flavours, generous moments, and something wonderful waiting at the table.</p>
            <div className="lux-hero-actions lux-hero-reveal">
              <button type="button" className="lux-button lux-button-light" onClick={() => goToSection('explore-menu')}>Explore the menu <span aria-hidden="true">↓</span></button>
              <button type="button" className="lux-button lux-button-outline" onClick={() => publicApi.showPage('customer-orders')}>View my orders</button>
              <span className="lux-hero-note">Prepared fresh. Served with care.</span>
            </div>
          </div>
          <div className="lux-hero-index"><span>01</span><i /> DineDesk</div>
          <a className="lux-scroll-cue" href="#explore-menu" aria-label="Scroll to explore the menu"><span /></a>
        </section>

        <section className="lux-intro lux-intro-single" id="kitchen-story">
          <Reveal className="lux-intro-copy">
            <p className="lux-eyebrow">The pleasure is in the details</p>
            <h2>Made with intention.<br /><em>Remembered for the feeling.</em></h2>
            <p>At DineDesk, the smallest details make the biggest difference. We bring a considered touch to every plate, with ingredients selected for their freshness and flavours made to be enjoyed slowly.</p>
          </Reveal>
        </section>

        <section className="lux-menu-section" id="explore-menu">
          <div className="lux-section-heading">
            <Reveal><p className="lux-eyebrow">A selection from our kitchen</p><h2>Explore the <em>menu.</em></h2></Reveal>
            <Reveal delay={100}><p>Made to order and meant to be savoured. Find a new favourite or return to an old one.</p></Reveal>
          </div>
          <div className="lux-menu-controls">
            <div className="lux-category-list" role="group" aria-label="Filter menu by category">
              {categories.map((value) => <button type="button" key={value} className={category === value ? 'selected' : ''} onClick={() => setCategory(value)}>{value}</button>)}
            </div>
            <button type="button" className="lux-button lux-button-dark" onClick={() => publicApi.showPage('orders')}>Order from the kitchen</button>
          </div>
          <div className="lux-food-grid">
            {visibleItems.length
              ? visibleItems.map((item, index) => <FoodCard
                  key={item.id || item.name}
                  item={item}
                  index={index}
                  onImageClick={(selectedItem, image) => setSelectedFoodImage({ item: selectedItem, image })}
                />)
              : <p className="lux-menu-empty">Our menu is being prepared. Please check back shortly.</p>}
          </div>
          <div className="lux-kitchen-cta">
            <div><p className="lux-eyebrow">Ready when you are</p><h3>Let us set the table.</h3></div>
            <button type="button" className="lux-button lux-button-light" onClick={() => publicApi.showPage('orders')}>Order from the kitchen</button>
          </div>
        </section>
      </main>

      <RestaurantFooter />
      <FoodImageDialog selection={selectedFoodImage} onClose={() => setSelectedFoodImage(null)} />
    </div>
  );
}

function KitchenPage() {
  const menuItems = useMenuItems();
  const cartItems = useCartItems();
  const [category, setCategory] = useState('All');
  const [query, setQuery] = useState('');
  const [orderType, setOrderType] = useState('Dine in');
  const [availableTables, setAvailableTables] = useState([]);
  const [selectedTable, setSelectedTable] = useState('');
  const [tableError, setTableError] = useState('');
  const [customerEmail, setCustomerEmail] = useState(() => publicApi.getCustomerOrderHistoryEmail());
  const count = cartItems.reduce((total, item) => total + item.quantity, 0);
  useReveal();
  useEffect(() => {
    let active = true;
    const loadTables = () => publicApi.getAvailableTables()
      .then((tables) => {
        if (active) {
          setAvailableTables(tables);
          setSelectedTable((current) => tables.includes(current) ? current : '');
          setTableError('');
        }
      })
      .catch(() => { if (active) setTableError('Tables are unavailable right now. Please try again shortly.'); });
    loadTables();
    window.addEventListener('dinedesk:order-updated', loadTables);
    const refreshTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible'
        && document.getElementById('page-orders')?.classList.contains('active')) loadTables();
    }, 10000);
    return () => {
      active = false;
      window.clearInterval(refreshTimer);
      window.removeEventListener('dinedesk:order-updated', loadTables);
    };
  }, []);
  const categories = getCategories(menuItems);
  const filteredItems = menuItems.filter((item) =>
    (category === 'All' || item.category === category) &&
    `${item.name} ${item.category} ${item.description || ''}`.toLowerCase().includes(query.trim().toLowerCase())
  );
  const total = cartItems.reduce((sum, item) => sum + Number(item.price) * item.quantity, 0);
  return (
    <div className="lux-site kitchen-site">
      <nav className="lux-nav kitchen-nav">
        <button type="button" className="lux-wordmark kitchen-wordmark" onClick={() => publicApi.showPage('login')} aria-label="DineDesk staff login"><BrandMark /><span>DineDesk</span><small>KITCHEN</small></button>
        <div className="lux-kitchen-nav-note"><span className="lux-open-dot" /> Made fresh to order</div>
        <button type="button" className="lux-kitchen-explore" onClick={() => publicApi.returnHome()} aria-label="Back to home" title="Back to home"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 10.5 9-7.5 9 7.5M5.5 9v11h13V9M9.5 20v-6h5v6" /></svg></button>
      </nav>
      <header className="kitchen-hero">
        <div className="kitchen-hero-image" />
        <div className="kitchen-hero-overlay" />
        <div className="kitchen-hero-copy">
          <p className="lux-eyebrow lux-hero-reveal"><span /> DineDesk kitchen</p>
          <h1 className="lux-hero-title lux-hero-reveal">The good part<br />starts <em>here.</em></h1>
          <p className="lux-hero-description lux-hero-reveal">Settle in, take your time, and choose something made just for you.</p>
        </div>
        <div className="kitchen-hero-mark">FRESH · CONSIDERED · YOURS</div>
      </header>

      <main className="kitchen-main">
        <section className="kitchen-menu-column" aria-labelledby="kitchen-menu-heading">
          <div className="kitchen-heading-row">
            <div><p className="lux-eyebrow">A taste of DineDesk</p><h2 id="kitchen-menu-heading">The <em>menu.</em></h2></div>
            <label className="kitchen-search"><span className="visually-hidden">Search dishes</span><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 5 5" /></svg><input type="search" placeholder="Find a favourite" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          </div>
          <div className="lux-category-list kitchen-categories" role="group" aria-label="Filter menu by category">
            {categories.map((value) => <button type="button" key={value} className={category === value ? 'selected' : ''} onClick={() => setCategory(value)}>{value}</button>)}
          </div>
          <div className="kitchen-food-grid">
            {filteredItems.length
              ? filteredItems.map((item, index) => <FoodCard key={item.id || item.name} item={item} index={index} compact />)
              : <p className="lux-menu-empty">We couldn’t find a match. Try another search.</p>}
          </div>
        </section>

        <aside className="kitchen-cart" id="kitchen-cart">
          <div className="kitchen-cart-top"><p className="lux-eyebrow">Your selection</p><h2>A table for <em>you.</em></h2><span>{count} item{count === 1 ? '' : 's'}</span></div>
          {cartItems.length ? <div className="kitchen-cart-items">
            {cartItems.map((item) => <article className="kitchen-cart-item" key={item.name}>
              <div className="kitchen-cart-item-image"><img src={item.image || publicApi.imageUrl(menuItems.find((menuItem) => menuItem.name === item.name) || item)} alt="" loading="eager" decoding="async" /></div>
              <div className="kitchen-cart-item-info"><strong>{item.name}</strong><span>R{Number(item.price).toFixed(2)} each</span><div className="kitchen-quantity"><button type="button" aria-label={`Remove one ${item.name}`} onClick={() => publicApi.changeQuantity(item.name, -1)}>−</button><span>{item.quantity}</span><button type="button" aria-label={`Add one ${item.name}`} onClick={() => publicApi.changeQuantity(item.name, 1)}>+</button></div></div>
              <strong className="kitchen-cart-item-total">R{(Number(item.price) * item.quantity).toFixed(2)}</strong>
            </article>)}
            <button type="button" className="kitchen-clear" onClick={() => publicApi.clearCart()}>Clear selection</button>
          </div> : <div className="kitchen-empty"><img src={cartBrandMark} alt="DineDesk" /><span>Your order will appear here.</span></div>}
          <div className="kitchen-cart-total"><span>Your total</span><strong>R{total.toFixed(2)}</strong></div>
          <div className="kitchen-choice"><label htmlFor="customer-email">Email for order history (optional)</label><input id="customer-email" type="email" autoComplete="email" maxLength={254} value={customerEmail} onChange={(event) => setCustomerEmail(event.target.value)} placeholder="you@example.com" /></div>
          <div className="kitchen-choice"><label htmlFor="order-type">How would you like it?</label><select id="order-type" value={orderType} onChange={(event) => setOrderType(event.target.value)}><option value="Dine in">Dine in</option><option value="Takeaway">Takeaway</option></select><p id="order-type-error" className="order-type-error" role="alert" /></div>
          {orderType === 'Dine in' && <div className="kitchen-choice"><label htmlFor="table-number">Choose your table</label><select id="table-number" value={selectedTable} onChange={(event) => setSelectedTable(event.target.value)}><option value="">Select an available table</option>{availableTables.map((table) => <option value={table} key={table}>Table {table}</option>)}</select><p className="order-type-error" role="alert">{tableError}</p></div>}
          <div className="kitchen-choice"><label htmlFor="payment-method">Payment preference</label><select id="payment-method" defaultValue={publicApi.getPaymentMethod()} onChange={(event) => publicApi.setPaymentMethod(event.target.value)}><option value="cash">Cash — pay your waiter</option><option value="card">Card — pay your waiter</option><option value="online">Pay securely online with Paystack</option></select></div>
          <button type="button" className="lux-button lux-button-dark kitchen-checkout" onClick={() => publicApi.placeOrder(customerEmail)}>Place your order</button>
          <p className="kitchen-cart-footnote">Prepared with care, and sent straight to our kitchen.</p>
        </aside>
      </main>
      <RestaurantFooter kitchen />
    </div>
  );
}

createRoot(document.getElementById('home-react-root')).render(<HomePage />);
createRoot(document.getElementById('kitchen-react-root')).render(<KitchenPage />);
